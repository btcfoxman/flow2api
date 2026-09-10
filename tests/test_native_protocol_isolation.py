import asyncio
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.core.config import config
from src.core.async_queue import AsyncQueueExpired
from src.core.generation_errors import NativeSessionError
from src.core.logger import debug_logger
from src.services.browser_captcha_native_cdp import NativeCdpAccountBrowser
from src.services.flow_client import FlowClient
import test_quota_account_switching as quota_helpers


class ProtocolIsolationTests(unittest.IsolatedAsyncioTestCase):
    def settings(self, families=()):
        return patch.dict(config._config, {'flow': {**config._config.get('flow', {}),
                          'angular_video_models': [], 'angular_video_families': list(families)}})

    async def test_legacy_page_does_not_require_angular_bootstrap(self):
        browser = NativeCdpAccountBrowser(1, None)
        browser._requires_flow_login = True
        browser.connection = SimpleNamespace(send=AsyncMock(return_value={}))
        browser._wait_for_document_ready = AsyncMock()
        browser._evaluate = AsyncMock(return_value='https://labs.google/fx/zh/tools/flow/project/p')
        await browser._open_real_project_page('s', 'p')
        self.assertEqual(browser._evaluate.await_count, 1)
        self.assertIn('labs.google/', browser.connection.send.await_args.args[1]['url'])

    async def test_angular_requires_bootstrap_even_without_cookie_field(self):
        browser = NativeCdpAccountBrowser(1, None)
        browser._evaluate = AsyncMock(side_effect=['https://flow.google.com/project/p', False])
        with self.assertRaisesRegex(NativeSessionError, 'flow_login_unavailable'):
            await browser._validate_project_page('s', 'p', 'angular')

    async def test_navigation_waits_for_new_document_and_bootstrap(self):
        browser = NativeCdpAccountBrowser(1, None)
        browser._evaluate = AsyncMock(side_effect=['about:blank', 'https://flow.google.com/project/p',
                                                  False, 'https://flow.google.com/project/p', True])
        with patch('src.services.browser_captcha_native_cdp.asyncio.sleep', AsyncMock()):
            await browser._validate_project_page('s', 'p', 'angular', wait_seconds=8)
        self.assertEqual(browser._evaluate.await_count, 5)

    async def test_rejects_wrong_origin_or_project_and_redacts_url(self):
        for url in ['https://flow.google.com.evil.test/project/p?token=secret',
                    'https://flow.google.com/project/other', 'https://accounts.google.com/login?secret=hidden']:
            browser = NativeCdpAccountBrowser(1, None)
            browser._evaluate = AsyncMock(return_value=url)
            with self.assertRaises(NativeSessionError) as caught:
                await browser._validate_project_page('s', 'p', 'angular')
            self.assertNotIn('secret', json.dumps(caught.exception.diagnostic()))
            self.assertNotIn('hidden', str(caught.exception))

    async def test_cache_isolated_by_protocol_and_revalidated(self):
        browser = NativeCdpAccountBrowser(1, None)
        browser._create_page_session = AsyncMock(side_effect=[('t1', 's1'), ('t2', 's2')])
        browser._seed_session_cookie = AsyncMock(return_value=False)
        browser._open_real_project_page = AsyncMock()
        browser._capture_fingerprint = AsyncMock()
        browser._evaluate = AsyncMock()
        browser._validate_project_page = AsyncMock()
        self.assertEqual(await browser._get_or_create_project_session('p', 'labs'), ('t1', 's1'))
        self.assertEqual(await browser._get_or_create_project_session('p', 'angular'), ('t2', 's2'))
        self.assertEqual(await browser._get_or_create_project_session('p', 'labs'), ('t1', 's1'))
        self.assertEqual(len(browser._project_sessions), 2)
        browser._validate_project_page.assert_awaited_once_with('s1', 'p', 'labs')

    async def test_migrated_legacy_page_uses_verified_angular_context_before_fetch(self):
        browser = NativeCdpAccountBrowser(1, None)
        browser.connection = SimpleNamespace(closed=False, send=AsyncMock())
        browser._create_page_session = AsyncMock(side_effect=[('t1','s1'),('t2','s2')])
        browser._seed_session_cookie = AsyncMock(return_value=True)
        browser._capture_fingerprint = AsyncMock()
        browser._open_real_project_page = AsyncMock(side_effect=[
            NativeSessionError('project_context_unavailable', page_url='https://flow.google.com/about'), None])
        result = await browser._get_or_create_project_session('p','labs')
        self.assertEqual(result, ('t2','s2'))
        self.assertTrue(browser._legacy_migrated)
        self.assertEqual(list(browser._project_sessions), [('angular','p')])
        browser._seed_session_cookie.assert_awaited_with('s2','angular')

    async def test_incomplete_angular_cookie_snapshot_fails_before_browser_mutation(self):
        browser = NativeCdpAccountBrowser(1, SimpleNamespace(get_token=AsyncMock(return_value=SimpleNamespace(
            st='unused',google_cookies=json.dumps([{'name':'OSID','value':'partial','domain':'flow.google.com'}])))))
        browser.connection=SimpleNamespace(send=AsyncMock())
        with self.assertRaisesRegex(NativeSessionError,'google_session_cookies_incomplete'):
            await browser._seed_session_cookie('s','angular')
        browser.connection.send.assert_not_awaited()

    async def test_complete_modern_session_skips_migrating_legacy_page(self):
        jar = json.dumps([{'name':'SID','value':'test','domain':'.google.com'},
                          {'name':'OSID','value':'test','domain':'flow.google.com'}])
        browser = NativeCdpAccountBrowser(1, SimpleNamespace(get_token=AsyncMock(
            return_value=SimpleNamespace(google_cookies=jar))))
        browser._create_page_session = AsyncMock(return_value=('t','s'))
        browser._seed_session_cookie = AsyncMock()
        browser._open_real_project_page = AsyncMock()
        browser._capture_fingerprint = AsyncMock()
        await browser._get_or_create_project_session('p', 'labs')
        browser._open_real_project_page.assert_awaited_once_with('s','p','angular')
        self.assertTrue(browser._legacy_migrated)

    async def test_document_wait_does_not_accept_transient_interactive_state(self):
        browser = NativeCdpAccountBrowser(1, None)
        browser._evaluate=AsyncMock(side_effect=['interactive','complete','loading','complete','complete','complete'])
        with patch('src.services.browser_captcha_native_cdp.asyncio.sleep', AsyncMock()):
            await browser._wait_for_document_ready('s')
        self.assertEqual(browser._evaluate.await_count, 6)

    async def test_legacy_does_not_import_google_cookies(self):
        jar = json.dumps([{'name':'SID', 'value':'private', 'domain':'.google.com', 'path':'/'}])
        browser = NativeCdpAccountBrowser(1, SimpleNamespace(get_token=AsyncMock(
            return_value=SimpleNamespace(st='test-st', google_cookies=jar))))
        browser.connection = SimpleNamespace(send=AsyncMock(return_value={'success':True}))
        with tempfile.TemporaryDirectory() as directory:
            browser.profile_dir = Path(directory)
            await browser._seed_session_cookie('s', 'labs')
            sets = [c.args[1]['name'] for c in browser.connection.send.await_args_list if c.args[0]=='Network.setCookie']
            self.assertEqual(sets, ['__Secure-next-auth.session-token'])
            self.assertFalse((Path(directory)/'.flow2api-google-cookie-seed').exists())

    async def test_image_upload_and_captcha_follow_same_model_protocol(self):
        client = FlowClient(None, db=object())
        service = SimpleNamespace(fetch_json=AsyncMock(return_value={'media':{'name':'media'}}),
                                  get_token=AsyncMock(return_value=('captcha','native:1')),
                                  get_fingerprint=lambda _: {})
        original = config.captcha_method
        config.set_captcha_method('native_cdp')
        self.addCleanup(config.set_captcha_method, original)
        with patch('src.services.browser_captcha_native_cdp.BrowserCaptchaService.get_instance', AsyncMock(return_value=service)):
            for families, expected in [((), 'labs'), (('abra_r2v',), 'angular')]:
                with self.settings(families):
                    for model in ['abra_r2v_4s_360p', 'abra_r2v_4s', 'abra_r2v_10s_720p']:
                        await client.upload_image('unused', b'\xff\xd8\xffdata', project_id='p', token_id=1, model_key=model)
                        await client._get_recaptcha_token('p', 'VIDEO_GENERATION', token_id=1, model_key=model)
                        self.assertEqual(service.fetch_json.await_args.kwargs['page_protocol'], expected)
                        self.assertEqual(service.get_token.await_args.kwargs['page_protocol'], expected)

    async def test_upload_preflight_is_not_rewrapped_or_retried(self):
        client = FlowClient(None, db=object())
        failure = NativeSessionError('flow_login_unavailable', protocol='angular')
        service = SimpleNamespace(fetch_json=AsyncMock(side_effect=failure))
        original = config.captcha_method
        config.set_captcha_method('native_cdp')
        self.addCleanup(config.set_captcha_method, original)
        with self.settings(), patch('src.services.browser_captcha_native_cdp.BrowserCaptchaService.get_instance', AsyncMock(return_value=service)):
            with self.assertRaises(NativeSessionError) as caught:
                await client.upload_image('unused', b'\xff\xd8\xffdata', project_id='p', token_id=1)
        self.assertIs(caught.exception, failure)
        self.assertEqual(service.fetch_json.await_count, 1)

    async def test_video_upload_does_not_switch_on_cookie_presence(self):
        client = FlowClient(None, db=SimpleNamespace(get_token=AsyncMock()))
        client._resolve_request_proxy = AsyncMock(side_effect=RuntimeError('legacy route reached'))
        original = config.captcha_method
        config.set_captcha_method('native_cdp')
        self.addCleanup(config.set_captcha_method, original)
        with self.settings(), self.assertRaisesRegex(RuntimeError, 'legacy route reached'):
            await client.upload_video_with_metadata('st', 'p', b'video', token_id=1, model_key='abra_edit_360p')
        client.db.get_token.assert_not_awaited()


class FailureAccountingTests(unittest.IsolatedAsyncioTestCase):
    async def test_preflight_and_wrapped_upload_do_not_disable_account(self):
        for failure, expected_status in [(AsyncQueueExpired(), 408),
              (NativeSessionError('flow_login_unavailable', protocol='angular'), 503),
              (RuntimeError('Project-scoped image upload failed via /flow/uploadImage (cause=HTTP Error 500)'), 502)]:
            handler, _, _ = quota_helpers.QuotaAccountSwitchingTests()._make_handler(quota_token_ids=set())
            from src.core.session_availability import SessionAvailability
            handler.token_manager.native_sessions = SessionAvailability()
            async def fail(*args, **kwargs):
                raise failure
                yield
            handler._handle_video_generation = fail
            chunks = [c async for c in handler.handle_generation('abra_r2v_4s_360p', 'test', images=[b'test'])]
            payload = json.loads(chunks[-1])
            self.assertEqual(payload['error']['status_code'], expected_status)
            handler.token_manager.record_error.assert_not_awaited()
            self.assertNotIn('native_session', json.dumps(payload))
            if isinstance(failure, NativeSessionError):
                self.assertEqual(handler._log_request.await_args.args[3]['internal_failure']['reason'], 'flow_login_unavailable')
                self.assertTrue(handler.token_manager.native_sessions._blocked)
            else:
                self.assertFalse(handler.token_manager.native_sessions._blocked)

    async def test_runtime_logging_survives_debug_off_and_excludes_unknown_fields(self):
        output = io.StringIO()
        with patch('src.core.logger.config', SimpleNamespace(debug_enabled=False)), redirect_stdout(output), patch.object(debug_logger.logger, 'warning') as logged:
            debug_logger.log_runtime_event('test_event', token_id=1, reason='page_not_ready', cookies='secret', authorization='secret')
        logged.assert_called_once()
        self.assertIn('page_not_ready', output.getvalue())
        self.assertNotIn('secret', output.getvalue())
