import asyncio
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.api.admin import plugin_update_token
from src.core.generation_errors import NativeSessionError
from src.services.browser_captcha_native_cdp import NativeCdpAccountBrowser, BrowserCaptchaService
from src.services.token_manager import TokenManager


def account(**changes):
    return SimpleNamespace(**{**dict(id=52, st='test-st', google_cookies='test-jar',
        captcha_proxy_url='socks5://localhost:20035', current_project_id='test-project',
        is_active=False), **changes})


class NativeSyncPreflightTests(unittest.IsolatedAsyncioTestCase):
    async def test_account_probe_failure_records_safe_diagnostics_and_releases_lock(self):
        worker=NativeCdpAccountBrowser(52,None)
        worker._flow_account_snapshot_locked=AsyncMock(side_effect=NativeSessionError('project_context_unavailable',
            protocol='angular',page_url='https://flow.google.com/about?secret=hidden'))
        with patch('src.services.browser_captcha_native_cdp.debug_logger.log_runtime_event') as event:
            with self.assertRaises(NativeSessionError):
                await worker.flow_account_snapshot('test@example.com')
        self.assertEqual(worker.last_error,'project_context_unavailable')
        self.assertNotIn('secret',str(event.call_args))
        self.assertEqual(event.call_args.kwargs['page_path'],'/about')
        self.assertFalse(worker.solve_lock.locked())
        worker._flow_account_snapshot_locked=AsyncMock(return_value={'email':'test@example.com'})
        await worker.flow_account_snapshot('test@example.com')
        self.assertIsNone(worker.last_error)

    async def test_slow_resource_does_not_reject_an_authenticated_exact_project(self):
        worker=NativeCdpAccountBrowser(52,None)
        worker.connection=SimpleNamespace(send=AsyncMock(return_value={}))
        worker._wait_for_document_ready=AsyncMock(side_effect=TimeoutError())
        worker._evaluate=AsyncMock(side_effect=['https://flow.google.com/project/test-project',True])
        await worker._open_real_project_page('session','test-project','angular')
        self.assertEqual(worker._evaluate.await_count,2)

    async def test_load_timeout_on_about_page_remains_a_signed_out_failure(self):
        worker=NativeCdpAccountBrowser(52,None)
        worker.connection=SimpleNamespace(send=AsyncMock(return_value={}))
        worker._wait_for_document_ready=AsyncMock(side_effect=TimeoutError())
        worker._evaluate=AsyncMock(return_value='https://flow.google.com/about')
        with self.assertRaises(NativeSessionError) as caught:
            await worker._open_real_project_page('session','test-project','angular')
        self.assertEqual(caught.exception.reason,'project_context_unavailable')

    async def test_page_check_does_not_solve_or_submit_and_uses_angular_context(self):
        worker = NativeCdpAccountBrowser(52, None)
        worker._prepare_profile = AsyncMock()
        worker._get_or_create_project_session = AsyncMock()
        worker.solve = AsyncMock()
        worker.fetch_json = AsyncMock()
        await worker.verify_session('test-project')
        worker._prepare_profile.assert_awaited_once_with(for_solve=False)
        worker._get_or_create_project_session.assert_awaited_once_with('test-project', 'angular')
        worker.solve.assert_not_awaited()
        worker.fetch_json.assert_not_awaited()
        self.assertFalse(worker.solve_lock.locked())

    async def test_success_unblocks_and_failure_does_not_expire_on_unchanged_session(self):
        token=account()
        db=SimpleNamespace(get_token=AsyncMock(return_value=token))
        manager=TokenManager(db, None)
        service=SimpleNamespace(verify_session=AsyncMock())
        with patch.object(BrowserCaptchaService, 'get_instance', AsyncMock(return_value=service)):
            self.assertTrue(await manager.verify_native_session(52))
            self.assertTrue(manager.native_sessions.available(token))
            service.verify_session.side_effect=NativeSessionError('project_context_unavailable', page_url='https://flow.google.com/about')
            with patch('src.core.native_session_state.local_session_state', return_value=None):
                self.assertFalse(await manager.verify_native_session(52))
            with patch('src.core.session_availability.time.monotonic', return_value=10**12):
                self.assertFalse(manager.native_sessions.available(token))

    async def test_stale_verification_cannot_approve_new_credentials(self):
        db=SimpleNamespace(get_token=AsyncMock(side_effect=[account(), account(st='new-st')]))
        manager=TokenManager(db, None)
        service=SimpleNamespace(verify_session=AsyncMock())
        with patch.object(BrowserCaptchaService, 'get_instance', AsyncMock(return_value=service)):
            self.assertFalse(await manager.verify_native_session(52))

    async def test_capacity_timeout_releases_busy_reservation(self):
        # Avoid starting a reaper or browser in this unit test.
        service=object.__new__(BrowserCaptchaService)
        service._closed=False
        worker=SimpleNamespace(busy_count=0, last_used_at=0, verify_session=AsyncMock())
        service._workers={52:worker}
        service._capacity_condition=asyncio.Condition()
        service._ensure_capacity=AsyncMock(side_effect=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            await service.verify_session(52, 'test-project')
        self.assertEqual(worker.busy_count, 0)
        worker.verify_session.assert_not_awaited()

    async def test_sync_does_not_auto_enable_until_target_preflight_passes(self):
        jar=[{'name':'SID','value':'test-only','domain':'.google.com','path':'/'},
             {'name':'OSID','value':'test-only','domain':'flow.google.com','path':'/'}]
        for verified in (False, True):
            db=SimpleNamespace(get_plugin_config=AsyncMock(return_value=SimpleNamespace(connection_token='test-key', auto_enable_on_update=True)),
                get_token_by_email=AsyncMock(return_value=account()))
            client=SimpleNamespace(credential_proxy_context=lambda value:nullcontext(),
                st_to_at=AsyncMock(return_value={'access_token':'test-at','expires':'2099-01-01T00:00:00Z','user':{'email':'test@example.invalid'}}),
                get_credits=AsyncMock(return_value={'credits':36}))
            manager=SimpleNamespace(flow_client=client, update_token=AsyncMock(), enable_token=AsyncMock(),
                verify_native_session=AsyncMock(return_value=verified))
            with patch('src.api.admin.db',db), patch('src.api.admin.token_manager',manager), \
                 patch('src.api.admin.config',SimpleNamespace(captcha_method='native_cdp')), \
                 patch('src.api.admin.local_session_state',return_value=None), \
                 patch('src.api.admin._normalize_plugin_captcha_proxy_url',return_value=('socks5://localhost:20035',True)):
                result=await plugin_update_token({'session_token':'test-st','google_cookies':jar},'Bearer test-key')
            self.assertTrue(result['oauth_verified'])
            self.assertTrue(result['cookies_updated'])
            self.assertEqual(result['native_session_verified'],verified)
            self.assertEqual(result['account_active'],verified)
            self.assertEqual(manager.enable_token.await_count,int(verified))
