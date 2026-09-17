import asyncio
import copy
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from src.api import admin
from src.api.account_login import COOKIE, register_account_login, asset_path
from src.core.config import config
from src.core.generation_errors import NativeSessionError
from src.core.media_static import MediaStaticFiles
from src.core.models import Token
from src.core.native_session_state import claim_local_session, local_session_state
from src.core.session_availability import SessionAvailability
from src.services.account_login import AccountLoginManager, LoginDesktop, LoginError
from src.services.browser_captcha_native_cdp import BrowserCaptchaService, NativeCdpAccountBrowser
from src.services.concurrency_manager import ConcurrencyManager
from src.services.load_balancer import LoadBalancer
from src.services.proxy_manager import ProxyManager


class FakeDesktop:
    available = staticmethod(lambda: True)

    def __init__(self):
        self.display, self.port, self.password = ':123', 5999, 'ephemeral'
        self.running = False

    async def start(self):
        self.running = True

    async def close(self):
        self.running = False

    def alive(self):
        return self.running


class AccountLoginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.addCleanup(config.set_captcha_method, config.captcha_method)
        config.set_captcha_method('native_cdp')
        self.account = Token(id=7, st='flow:existing', email='one@example.test', auth_mode='flow',
                             captcha_proxy_url='socks5://localhost:20001', google_cookies='old-export', credits=50)
        async def get_token(token_id):
            return copy.deepcopy(self.account) if token_id == 7 else None
        async def update(token_id, **values):
            for key, value in values.items():
                setattr(self.account, key, value)
        self.db = SimpleNamespace(get_token=AsyncMock(side_effect=get_token), update_token=AsyncMock(side_effect=update),
            get_all_tokens=AsyncMock(return_value=[self.account]), get_active_tokens=AsyncMock(return_value=[self.account]))
        async def enable(token_id):
            await update(token_id, is_active=True)
        self.tokens = SimpleNamespace(_flow_sync_lock=asyncio.Lock(), native_sessions=SessionAvailability(),
            _flow_verified={}, enable_token=AsyncMock(side_effect=enable), _refresh_credits_with_status=AsyncMock())
        self.concurrency = ConcurrencyManager()
        self.load_balancer = LoadBalancer(self.tokens, self.concurrency)
        self.worker = NativeCdpAccountBrowser(7, self.db)
        self.worker.profile_dir = Path(self.folder.name) / 'token-7'
        self.worker.profile_dir.mkdir()
        (self.worker.profile_dir / 'keep-existing-profile').write_text('keep', encoding='utf-8')
        self.worker.connection = SimpleNamespace(send=AsyncMock(), closed=False)
        self.worker.stop = AsyncMock()
        self.worker._create_page_session = AsyncMock(return_value=('target', 'page'))
        self.worker.flow_account_snapshot = AsyncMock(return_value={
            'email':'one@example.test', 'credits':42, 'userPaygateTier':'PAYGATE_TIER_ONE',
            'projects':[{'project_id':'project-1', 'project_name':'Main'}], 'google_cookies':'must-not-copy'})
        self.service = SimpleNamespace(_workers={7:self.worker}, _ensure_capacity=AsyncMock())
        self.valid = {'owner'}
        self.manager = AccountLoginManager(self.db, self.tokens, self.concurrency,
                                          lambda token: token in self.valid, self.load_balancer)
        for target, replacement in [
            ('src.services.account_login.LoginDesktop', FakeDesktop),
            ('src.services.account_login.BrowserCaptchaService.get_instance', AsyncMock(return_value=self.service)),
            ('src.services.account_login.local_session_state', lambda token_id, profile_dir=None:
                local_session_state(token_id, profile_dir or self.worker.profile_dir))]:
            patcher = patch(target, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addAsyncCleanup(self.manager.close)

    async def start(self):
        return await self.manager.start(7, 'owner')

    async def test_login_reuses_exact_profile_and_blocks_generation_without_copying_cookies(self):
        self.tokens.native_sessions.verified(self.account)
        result = await self.start()
        self.assertEqual(result['active']['token_id'], 7)
        self.assertFalse(self.account.is_active)
        self.assertIsNone(self.account.google_cookies)
        self.assertTrue(local_session_state(7, self.worker.profile_dir))
        self.assertTrue((self.worker.profile_dir / 'keep-existing-profile').is_file())
        self.assertEqual(self.worker.display_override, ':123')
        self.assertTrue(self.worker.is_busy)
        self.assertFalse(await self.concurrency.acquire_image(7))
        self.assertFalse(await self.concurrency.acquire_video(7))
        self.assertFalse(await self.load_balancer._add_pending(7, True, False))
        self.assertTrue(await self.concurrency.acquire_image(8))
        self.assertEqual(self.tokens.native_sessions.status(self.account)['session_status'], 'unknown')

    async def test_finish_verifies_after_viewer_revocation_and_browser_close(self):
        await self.start()
        active = self.manager.active
        ticket = active['ticket']
        self.assertIs(self.manager.viewer(ticket), active)
        async def snapshot(email):
            self.assertEqual(email, self.account.email)
            self.assertFalse(active['desktop'].alive())
            self.assertIsNone(self.manager.viewer(ticket))
            self.assertIsNone(self.worker.display_override)
            self.assertTrue(self.worker.interactive_login)
            return {'credits':42, 'userPaygateTier':'PAYGATE_TIER_ONE', 'projects':[], 'google_cookies':'not-exported'}
        self.worker.flow_account_snapshot.side_effect = snapshot
        result = await self.manager.finish(active['id'], 'owner')
        self.assertEqual(result['session_status'], 'verified')
        self.assertTrue(self.account.is_active)
        self.assertIsNone(self.account.google_cookies)
        self.assertFalse(self.worker.interactive_login)
        self.assertIsNone(self.manager.active)
        self.assertTrue(await self.concurrency.acquire_video(7))
        self.assertTrue(await self.load_balancer._add_pending(7, False, True))
        self.assertEqual(self.tokens.native_sessions.status(self.account)['session_status'], 'verified')

    async def test_failed_identity_probe_never_enables_and_releases_resources(self):
        await self.start()
        active = self.manager.active
        self.worker.flow_account_snapshot.side_effect = NativeSessionError('flow_identity_mismatch')
        with self.assertRaises(NativeSessionError):
            await self.manager.finish(active['id'], 'owner')
        self.tokens.enable_token.assert_not_awaited()
        self.assertFalse(self.account.is_active)
        self.assertIsNone(self.manager.active)
        self.assertFalse(self.worker.interactive_login)
        self.assertFalse(active['desktop'].alive())

    async def test_cancel_keeps_profile_but_not_stale_enabled_state(self):
        await self.start()
        active = self.manager.active
        await self.manager.cancel(active['id'], 'owner')
        self.assertFalse(self.account.is_active)
        self.worker.flow_account_snapshot.assert_not_awaited()
        self.assertIsNone(self.manager.viewer(active['ticket']))
        self.assertTrue(local_session_state(7, self.worker.profile_dir))
        self.assertTrue((self.worker.profile_dir / 'keep-existing-profile').exists())

    async def test_pending_generation_cannot_be_interrupted_by_login(self):
        await self.load_balancer._add_pending(7, False, True)
        with self.assertRaises(LoginError):
            await self.start()
        self.db.update_token.assert_not_awaited()
        self.worker.stop.assert_not_awaited()

    async def test_inflight_generation_cannot_be_interrupted_by_login(self):
        await self.concurrency.acquire_image(7)
        with self.assertRaises(LoginError):
            await self.start()
        self.db.update_token.assert_not_awaited()
        self.assertNotIn(7, self.load_balancer._login_paused)

    async def test_probe_busy_prevents_adoption_and_releases_reservation(self):
        self.worker.busy_count = 1
        with self.assertRaises(LoginError):
            await self.start()
        self.assertNotIn(7, self.concurrency._login_paused)
        self.assertNotIn(7, self.load_balancer._login_paused)
        self.db.update_token.assert_not_awaited()

    async def test_startup_failure_leaves_disabled_account_and_no_leaked_desktop(self):
        self.worker._create_page_session.side_effect = RuntimeError('simulated')
        with self.assertRaises(RuntimeError):
            await self.start()
        self.assertIsNone(self.manager.active)
        self.assertFalse(self.worker.interactive_login)
        self.assertIsNone(self.worker.display_override)
        self.assertFalse(self.account.is_active)
        self.assertTrue(await self.concurrency.can_use_image(7))

    async def test_start_cancellation_cleans_up_and_preserves_profile(self):
        entered = asyncio.Event()
        async def wait():
            entered.set()
            await asyncio.Event().wait()
        self.worker._create_page_session.side_effect = wait
        task = asyncio.create_task(self.start())
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIsNone(self.manager.active)
        self.assertFalse(self.worker.interactive_login)
        self.assertTrue((self.worker.profile_dir / 'keep-existing-profile').exists())

    async def test_another_admin_cannot_view_or_finish_lease(self):
        await self.start()
        with self.assertRaises(LoginError):
            await self.manager.finish(self.manager.active['id'], 'other')
        self.assertFalse(self.manager.status('other')['active']['owned'])
        self.assertIsNone(self.manager.viewer('wrong'))

    async def test_expiry_and_logout_revoke_viewer_immediately(self):
        await self.start()
        active = self.manager.active
        self.valid.clear()
        self.assertIsNone(self.manager.viewer(active['ticket']))
        self.valid.add('owner')
        active['deadline'] = time.monotonic() - 1
        self.assertIsNone(self.manager.viewer(active['ticket']))

    async def test_single_login_and_shutdown_cleanup(self):
        await self.start()
        with self.assertRaises(LoginError):
            await self.start()
        active = self.manager.active
        await self.manager.close()
        self.assertFalse(active['desktop'].alive())
        self.assertFalse(self.worker.interactive_login)
        self.assertIsNone(self.manager.active)

    async def test_expired_window_is_reaped_without_enabling_account(self):
        await self.start()
        self.manager._watchdog.cancel()
        await asyncio.gather(self.manager._watchdog, return_exceptions=True)
        active = self.manager.active
        active['deadline'] = time.monotonic() - 1
        with patch('src.services.account_login.asyncio.sleep', AsyncMock()):
            await self.manager._watch()
        self.assertIsNone(self.manager.active)
        self.assertFalse(self.account.is_active)
        self.assertFalse(active['desktop'].alive())
        self.assertFalse(self.worker.interactive_login)

    async def test_proxy_binding_changes_fail_without_erasing_profile(self):
        claim_local_session(7, 'socks5://other:20002', self.worker.profile_dir)
        with self.assertRaisesRegex(NativeSessionError, 'local_session_proxy_changed'):
            await self.start()
        self.db.update_token.assert_not_awaited()
        self.assertTrue((self.worker.profile_dir / 'keep-existing-profile').exists())
        self.assertIsNone(self.manager.active)

    async def test_maintenance_reuses_shared_probe_and_skips_recent_busy_and_disabled(self):
        claim_local_session(7, self.account.captcha_proxy_url, self.worker.profile_dir)
        await self.manager.check_accounts()
        self.tokens._refresh_credits_with_status.assert_awaited_once_with(7)
        self.tokens._refresh_credits_with_status.reset_mock()
        self.tokens.native_sessions.verified(self.account)
        await self.manager.check_accounts()
        self.tokens._refresh_credits_with_status.assert_not_awaited()
        self.tokens.native_sessions.discard(7)
        self.worker.busy_count = 1
        await self.manager.check_accounts()
        self.worker.busy_count = 0
        self.account.is_active = False
        await self.manager.check_accounts()
        self.tokens._refresh_credits_with_status.assert_not_awaited()

    async def test_native_service_entrypoints_reject_interactive_profile(self):
        service = BrowserCaptchaService(self.db)
        self.addAsyncCleanup(service.close)
        service._workers[7] = self.worker
        self.worker.interactive_login = True
        calls = [service.flow_account_snapshot(7, self.account.email), service.verify_session(7, ''),
                 service.get_token('', token_id=7), service.fetch_json(token_id=7, project_id='', url='https://unused')]
        for call in calls:
            with self.assertRaisesRegex(NativeSessionError, 'flow_account_busy'):
                await call
        self.worker.flow_account_snapshot.assert_not_awaited()

    async def test_deleted_account_probe_does_not_cancel_maintenance_loop(self):
        claim_local_session(7, self.account.captcha_proxy_url, self.worker.profile_dir)
        self.tokens._refresh_credits_with_status.side_effect = asyncio.CancelledError()
        await self.manager.check_accounts()
        self.assertFalse(asyncio.current_task().cancelling())


class DesktopReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_banner_is_closed_before_retry(self):
        desktop = LoginDesktop()
        self.addAsyncCleanup(desktop.close)
        process = SimpleNamespace(returncode=None)
        xvfb = SimpleNamespace(returncode=None, stdout=SimpleNamespace(readline=AsyncMock(return_value=b'123\n')))
        broken = SimpleNamespace(readexactly=AsyncMock(side_effect=asyncio.IncompleteReadError(b'RFB', 12)))
        ready = SimpleNamespace(readexactly=AsyncMock(return_value=b'RFB 003.008\n'))
        writers = [SimpleNamespace(close=Mock(), wait_closed=AsyncMock()) for _ in range(2)]
        async def connect(*args):
            if connect.calls:
                writers[0].close.assert_called_once()
            index = connect.calls
            connect.calls += 1
            return (broken if index == 0 else ready), writers[index]
        connect.calls = 0
        with patch.object(desktop, 'available', return_value=True), \
             patch.object(desktop, '_spawn', AsyncMock(side_effect=[xvfb, process, process])), \
             patch('src.services.account_login.asyncio.open_connection', side_effect=connect), \
             patch('src.services.account_login.asyncio.sleep', AsyncMock()):
            await desktop.start()
        for writer in writers:
            writer.close.assert_called_once()
            writer.wait_closed.assert_awaited_once()


class AccountLoginApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.manager = SimpleNamespace(status=Mock(return_value={'available':True, 'active':None}),
            start=AsyncMock(return_value={'active':None}), finish=AsyncMock(), cancel=AsyncMock(),
            viewer=Mock(return_value=None), _owned=Mock(side_effect=LoginError('not owned')))
        self.app = FastAPI()
        register_account_login(self.app, self.manager)
        self.client = AsyncClient(transport=ASGITransport(app=self.app), base_url='https://local')
        self.addAsyncCleanup(self.client.aclose)

    def authorize(self):
        self.app.dependency_overrides[admin.verify_admin_token] = lambda: 'owner'

    async def test_admin_required_and_cross_origin_rejected(self):
        for path in ['/api/account-login', '/account-login/credentials', '/account-login/viewer']:
            self.assertEqual((await self.client.get(path)).status_code, 401)
        self.authorize()
        self.assertEqual((await self.client.get('/api/account-login', headers={'Origin':'https://attacker'})).status_code, 403)
        result = await self.client.get('/api/account-login', headers={'Origin':'https://local'})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.headers['cache-control'], 'no-store')

    async def test_errors_never_expose_upstream_secrets(self):
        self.authorize()
        for error, status in [(RuntimeError('secret-cookie'), 503), (LoginError('busy'), 409),
                              (NativeSessionError('flow_identity_mismatch'), 409)]:
            self.manager.start.side_effect = error
            response = await self.client.post('/api/accounts/7/login')
            self.assertEqual(response.status_code, status)
            self.assertNotIn('secret-cookie', response.text)

    async def test_viewer_cookie_is_scoped_http_only_secure_and_not_admin_token(self):
        self.authorize()
        self.manager._owned.side_effect = None
        self.manager._owned.return_value = {'phase':'login','ticket':'ephemeral-viewer','deadline':time.monotonic()+10}
        response = await self.client.post('/api/account-login/session/viewer')
        self.assertEqual(response.status_code, 200)
        cookie = response.headers['set-cookie']
        for value in ['HttpOnly', 'Secure', 'SameSite=strict', 'Path=/account-login']:
            self.assertIn(value, cookie)
        self.assertNotIn('ephemeral-viewer', response.text)
        self.assertEqual(response.json()['url'], '/account-login/viewer')

    async def test_create_account_requires_proxy_and_does_not_enable(self):
        self.authorize()
        with tempfile.TemporaryDirectory() as folder, patch.dict('os.environ', {'NATIVE_CDP_PROFILE_ROOT':folder}), \
             patch.object(admin, 'proxy_manager', ProxyManager(None)):
            self.manager.db = SimpleNamespace(get_all_tokens=AsyncMock(return_value=[]), add_token=AsyncMock(return_value=4))
            self.manager.tokens = SimpleNamespace(_flow_sync_lock=asyncio.Lock())
            previous = config.captcha_method
            config.set_captcha_method('native_cdp')
            try:
                response = await self.client.post('/api/accounts', json={'email':'New@Example.test', 'captcha_proxy_url':'socks5://localhost:20001'})
                self.assertEqual(response.status_code, 200, response.text)
                account = self.manager.db.add_token.await_args.args[0]
                self.assertFalse(account.is_active)
                self.assertEqual(account.auth_mode, 'flow')
                self.assertEqual(account.email, 'new@example.test')
                self.assertTrue(local_session_state(4))
                response = await self.client.post('/api/accounts', json={'email':'other@example.test', 'captcha_proxy_url':'invalid'})
                self.assertEqual(response.status_code, 400)
            finally:
                config.set_captcha_method(previous)

    async def test_manual_enable_cannot_bypass_local_session_verification(self):
        manager = SimpleNamespace(verify_native_session=AsyncMock(return_value=False), enable_token=AsyncMock())
        with patch.object(admin, 'token_manager', manager), patch.object(admin, 'local_session_state', return_value={'version':1}):
            from fastapi import HTTPException
            with self.assertRaises(HTTPException) as caught:
                await admin.enable_token(7, 'owner')
            self.assertEqual(caught.exception.status_code, 409)
            manager.enable_token.assert_not_awaited()
            manager.verify_native_session.return_value = True
            await admin.enable_token(7, 'owner')
            manager.enable_token.assert_awaited_once_with(7)


class AccountLoginSecurityTests(unittest.TestCase):
    def test_websocket_proxies_only_the_active_loopback_endpoint_and_revokes(self):
        reader, writer = SimpleNamespace(), SimpleNamespace()
        reads = 0
        async def read(_):
            nonlocal reads
            reads += 1
            if reads == 1:
                return b'RFB 003.008\n'
            await asyncio.Event().wait()
        reader.read = read
        writer.write = Mock()
        writer.drain = AsyncMock()
        writer.close = Mock()
        writer.wait_closed = AsyncMock()
        lease = {'desktop':SimpleNamespace(port=5998)}
        manager = SimpleNamespace(viewer=Mock(return_value=lease))
        app = FastAPI()
        register_account_login(app, manager)
        connect = AsyncMock(return_value=(reader, writer))
        with patch('src.api.account_login.asyncio.open_connection', connect), TestClient(app) as client:
            with client.websocket_connect('/account-login/websockify', headers={
                    'Origin':'http://testserver', 'Cookie':f'{COOKIE}=ticket'}) as websocket:
                self.assertEqual(websocket.receive_bytes(), b'RFB 003.008\n')
                connect.assert_awaited_once_with('127.0.0.1', 5998)
                manager.viewer.return_value = None
                websocket.send_bytes(b'must-not-forward-after-revocation')
                self.assertEqual(websocket.receive()['type'], 'websocket.close')
        writer.write.assert_not_called()
        writer.close.assert_called_once()

    def test_websocket_requires_origin_and_live_cookie(self):
        manager = SimpleNamespace(viewer=Mock(return_value=None))
        app = FastAPI()
        register_account_login(app, manager)
        with TestClient(app) as client:
            from starlette.websockets import WebSocketDisconnect
            with self.assertRaises(WebSocketDisconnect):
                with client.websocket_connect('/account-login/websockify'):
                    self.fail('unauthorized websocket accepted')

    def test_novnc_assets_reject_traversal_and_nonlibrary_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/'core').mkdir()
            (root/'core'/'rfb.js').write_text('// fixture')
            (root/'secret').write_text('private')
            with patch('src.api.account_login.NOVNC_ROOT', root):
                self.assertEqual(asset_path('core/rfb.js'), root/'core'/'rfb.js')
                for path in ['../secret','core/../../secret', 'secret','core\\rfb.js', '/secret', '']:
                    with self.assertRaises(Exception):
                        asset_path(path)

    def test_stale_verification_is_not_presented_as_current(self):
        token = Token(id=1, st='flow:test', auth_mode='flow', email='test@example.test')
        state = SessionAvailability()
        state.verified(token)
        state._observations[1][1]['session_checked_at'] = (datetime.now(timezone.utc)-timedelta(minutes=16)).isoformat()
        self.assertEqual(state.status(token)['session_status'], 'stale')
        self.assertTrue(state.available(token))
        state.reject(token, NativeSessionError('flow_account_busy'))
        self.assertTrue(state.available(token))


class MediaSecurityTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_media_cannot_read_browser_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/'image.png').write_bytes(b'media')
            (root/'native_cdp_profiles').mkdir()
            (root/'native_cdp_profiles'/'Cookies').write_bytes(b'secret')
            (root/'native_cdp_profiles'/'cover.png').write_bytes(b'secret')
            (root/'.private.png').write_bytes(b'secret')
            app = FastAPI()
            app.mount('/tmp', MediaStaticFiles(directory=folder))
            async with AsyncClient(transport=ASGITransport(app=app), base_url='http://local') as client:
                self.assertEqual((await client.get('/tmp/image.png')).content, b'media')
                for path in ['native_cdp_profiles/Cookies', 'native_cdp_profiles/cover.png', '.private.png',
                             'native_cdp_profiles%2FCookies', 'native_cdp_profiles%5CCookies']:
                    result = await client.get('/tmp/'+path)
                    self.assertEqual(result.status_code, 404, path)
                    self.assertNotIn(b'secret', result.content)


@unittest.skipUnless(LoginDesktop.available(), 'requires Linux headed desktop packages (CI installs them)')
class LoginDesktopSmokeTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_isolated_desktop_requires_rfb_password_and_reaps_all_children(self):
        desktop = LoginDesktop()
        self.addAsyncCleanup(desktop.close)
        await desktop.start()
        processes = list(desktop.processes)
        self.assertTrue(desktop.alive())
        self.assertEqual(len(desktop.password), 8)
        reader, writer = await asyncio.open_connection('127.0.0.1', desktop.port)
        try:
            self.assertTrue((await asyncio.wait_for(reader.readexactly(12), 3)).startswith(b'RFB '))
            writer.write(b'RFB 003.008\n')
            await writer.drain()
            count = (await asyncio.wait_for(reader.readexactly(1), 3))[0]
            security_types = await asyncio.wait_for(reader.readexactly(count), 3)
            self.assertNotIn(1, security_types)  # No anonymous RFB authentication.
            self.assertIn(2, security_types)
        finally:
            writer.close()
            await writer.wait_closed()
        await desktop.close()
        self.assertTrue(all(p.returncode is not None for p in processes))
        self.assertFalse(desktop.alive())
        self.assertIsNone(desktop.folder)
