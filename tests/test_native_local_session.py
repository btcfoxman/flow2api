import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from fastapi import HTTPException

from src.core.generation_errors import NativeSessionError
from src.core.native_session_state import LOCAL_SESSION_MARKER, local_session_state
from src.services.browser_captcha_native_cdp import NativeCdpAccountBrowser, ProxyBinding
from src.api.admin import plugin_update_token
from src.core.config import config
from src.services.flow_client import FlowClient
from src.services.token_manager import TokenManager


class NativeLocalSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_waits_for_browser_exit_before_signalling(self):
        worker = self.worker()
        connection = SimpleNamespace(send=AsyncMock(), close=AsyncMock())
        process = Mock()
        process.poll.return_value = None
        process.wait.return_value = 0
        worker.connection, worker.process = connection, process
        await worker.stop(reason='test')
        connection.send.assert_awaited_once_with('Browser.close', timeout=3)
        process.wait.assert_called_once_with(timeout=8)
        process.terminate.assert_not_called()
        process.kill.assert_not_called()
        self.assertTrue((worker.profile_dir / LOCAL_SESSION_MARKER).exists())

    async def test_shutdown_terminates_only_after_grace_period(self):
        worker = self.worker()
        process = Mock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired('chromium',8),0]
        worker.process = process
        await worker.stop(reason='test')
        self.assertEqual([call.kwargs for call in process.wait.call_args_list],[{'timeout':8},{'timeout':5}])
        process.terminate.assert_called_once()
        process.kill.assert_not_called()

    async def test_shutdown_kills_only_after_terminate_timeout(self):
        worker = self.worker()
        process = Mock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired('chromium',8),subprocess.TimeoutExpired('chromium',5),0]
        worker.process = process
        await worker.stop(reason='test')
        self.assertEqual([call.kwargs for call in process.wait.call_args_list],[{'timeout':8},{'timeout':5},{'timeout':3}])
        process.terminate.assert_called_once()
        process.kill.assert_called_once()

    def worker(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        worker = NativeCdpAccountBrowser(43, SimpleNamespace(get_token=AsyncMock(return_value=SimpleNamespace(google_cookies=None))))
        worker.profile_dir = Path(folder.name)
        marker = {'version': 1, 'token_id': 43, 'proxy_sha256': hashlib.sha256(b'socks5://127.0.0.1:20001').hexdigest()}
        (worker.profile_dir / LOCAL_SESSION_MARKER).write_text(json.dumps(marker))
        return worker

    async def test_local_profile_never_imports_external_credentials(self):
        worker = self.worker()
        worker.connection = SimpleNamespace(send=AsyncMock())
        for protocol in ['labs', 'angular']:
            self.assertFalse(await worker._seed_session_cookie('session', protocol))
        worker.connection.send.assert_not_awaited()
        worker.db.get_token.assert_not_awaited()

    async def test_corrupt_marker_does_not_fall_back_to_cookie_import(self):
        worker = self.worker()
        worker.connection = SimpleNamespace(send=AsyncMock())
        (worker.profile_dir / LOCAL_SESSION_MARKER).write_text('{invalid')
        with self.assertRaises(NativeSessionError):
            await worker._seed_session_cookie('session')
        worker.connection.send.assert_not_awaited()

    async def test_marker_is_bound_to_account(self):
        worker = self.worker()
        with self.assertRaises(NativeSessionError):
            local_session_state(44, worker.profile_dir)

    async def test_risk_recovery_restarts_without_erasing_local_profile(self):
        worker = self.worker()
        worker._profile_reset_pending = True
        worker._profile_reset_reason = 'recaptcha_risk'
        worker.stop = AsyncMock()
        worker.start = AsyncMock()
        worker._reset_profile = AsyncMock()
        await worker._prepare_profile(for_solve=True)
        worker._reset_profile.assert_not_awaited()
        worker.stop.assert_awaited_once()
        self.assertTrue((worker.profile_dir / LOCAL_SESSION_MARKER).exists())

    async def test_proxy_change_is_rejected_before_profile_deletion(self):
        worker = self.worker()
        worker._resolve_proxy = AsyncMock(return_value=ProxyBinding('socks5://127.0.0.1:20002', 'token'))
        worker._reset_profile = AsyncMock()
        with self.assertRaisesRegex(NativeSessionError, 'local_session_proxy_changed'):
            await worker.start()
        worker._reset_profile.assert_not_awaited()

    async def test_local_profile_selects_modern_page_without_synced_cookie_field(self):
        worker = self.worker()
        worker._create_page_session = AsyncMock(return_value=('target', 'session'))
        worker._seed_session_cookie = AsyncMock(return_value=False)
        worker._open_real_project_page = AsyncMock()
        worker._capture_fingerprint = AsyncMock()
        await worker._get_or_create_project_session('project', 'labs')
        worker._open_real_project_page.assert_awaited_once_with('session', 'project', 'angular')

    async def test_sync_cannot_overwrite_or_auto_enable_local_account(self):
        database = SimpleNamespace(
            get_plugin_config=AsyncMock(return_value=SimpleNamespace(connection_token='test-key',auto_enable_on_update=True)),
            get_token_by_email=AsyncMock(return_value=SimpleNamespace(id=43,is_active=False)))
        manager=SimpleNamespace(flow_client=SimpleNamespace(st_to_at=AsyncMock(return_value={
            'access_token':'external-at','user':{'email':'local@example.test'}})),
            update_token=AsyncMock(),enable_token=AsyncMock())
        previous=config.captcha_method
        config.set_captcha_method('native_cdp')
        self.addCleanup(config.set_captcha_method,previous)
        with patch('src.api.admin.db',database), patch('src.api.admin.token_manager',manager), \
             patch('src.api.admin.local_session_state',return_value={'version':1}):
            with self.assertRaises(HTTPException) as caught:
                await plugin_update_token({'session_token':'external-st'}, 'Bearer test-key')
        self.assertEqual(caught.exception.status_code,409)
        manager.update_token.assert_not_awaited()
        manager.enable_token.assert_not_awaited()

    async def test_native_auth_never_falls_back_to_direct_connection(self):
        client=FlowClient(None)
        client._make_request=AsyncMock(side_effect=RuntimeError('Failed to connect to proxy'))
        previous=config.captcha_method
        config.set_captcha_method('native_cdp')
        self.addCleanup(config.set_captcha_method,previous)
        with self.assertRaises(RuntimeError):
            await client.st_to_at('test-st')
        self.assertEqual(client._make_request.await_count,1)
        self.assertFalse(client._make_request.await_args.kwargs.get('force_no_proxy',False))

    async def test_refresh_proxy_is_account_bound_and_context_is_restored(self):
        db=SimpleNamespace(get_token=AsyncMock(return_value=SimpleNamespace(captcha_proxy_url='socks5://127.0.0.1:20001')))
        client=FlowClient(None,db=db)
        client._set_request_fingerprint({'proxy_url':'socks5://old-context:1','native_token_id':9})
        manager=TokenManager(db,client)
        previous=config.captcha_method
        config.set_captcha_method('native_cdp')
        self.addCleanup(config.set_captcha_method,previous)
        async def refresh(*args):
            self.assertEqual(client.get_request_fingerprint(),{'proxy_url':'socks5://127.0.0.1:20001'})
            raise RuntimeError('simulated error')
        manager._do_refresh_at_on_bound_proxy=refresh
        with self.assertRaises(RuntimeError):
            await manager._do_refresh_at(43,'test-st')
        self.assertEqual(client.get_request_fingerprint(),{'proxy_url':'socks5://old-context:1','native_token_id':9})

    async def test_balance_and_project_control_calls_bind_native_account_proxy(self):
        db=SimpleNamespace(get_token=AsyncMock(return_value=SimpleNamespace(captcha_proxy_url='socks5://127.0.0.1:20001')))
        client=FlowClient(None,db=db)
        manager=TokenManager(db,client)
        previous=config.captcha_method
        config.set_captcha_method('native_cdp')
        self.addCleanup(config.set_captcha_method,previous)
        async def control(token_id):
            self.assertEqual(token_id,43)
            self.assertEqual(client.get_request_fingerprint(),{'proxy_url':'socks5://127.0.0.1:20001'})
            return 'checked'
        manager._refresh_credits_on_bound_proxy=control
        manager._ensure_project_exists_on_bound_proxy=control
        self.assertEqual(await manager._refresh_credits_inner(43),'checked')
        self.assertEqual(await manager.ensure_project_exists(43),'checked')
        self.assertIsNone(client.get_request_fingerprint())
