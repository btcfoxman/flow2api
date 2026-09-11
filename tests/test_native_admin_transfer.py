import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from src.api import admin
from src.core.config import config
from src.core.models import Token
from src.core.generation_errors import NativeSessionError

JAR=json.dumps([{"name":"SID","value":"root-secret","domain":".google.com","path":"/"},
                {"name":"OSID","value":"flow-secret","domain":"flow.google.com","path":"/"}])


class NativeAdminTransferTests(unittest.IsolatedAsyncioTestCase):
    def account(self,mode="flow"):
        return Token(id=7,st="flow:internal-identity" if mode=="flow" else "legacy-st",email="user@example.com",
            auth_mode=mode,google_cookies=JAR, captcha_proxy_url="socks5://localhost:20001",video_concurrency=0)

    async def test_explicit_export_is_admin_only_and_noncacheable(self):
        app=FastAPI(); app.include_router(admin.router)
        with patch.object(admin,"token_manager",SimpleNamespace(get_all_tokens=AsyncMock(return_value=[self.account(),self.account("labs")]))):
            async with AsyncClient(transport=ASGITransport(app=app),base_url="http://local") as client:
                self.assertEqual((await client.get('/api/tokens/export')).status_code,401)
                app.dependency_overrides[admin.verify_admin_token]=lambda:'test'
                response=await client.get('/api/tokens/export')
        self.assertEqual(response.headers['cache-control'],'no-store')
        rows=response.json()
        self.assertEqual(rows[0]['auth_mode'],'flow')
        self.assertIsNone(rows[0]['session_token'])
        self.assertEqual(rows[0]['google_cookies'],JAR)
        self.assertEqual(rows[0]['video_concurrency'],0)
        self.assertNotIn('internal-identity',response.text)
        self.assertEqual(rows[1]['session_token'],'legacy-st')
        self.assertNotIn('google_cookies',rows[1])

    async def import_flow(self,*,verified=True,error=None,active=True):
        manager=SimpleNamespace(get_all_tokens=AsyncMock(return_value=[]),get_token=AsyncMock(return_value=self.account()),
            flow_client=SimpleNamespace(st_to_at=AsyncMock(side_effect=AssertionError('Labs forbidden'))),
            sync_flow_session=AsyncMock(return_value={'token_id':7,'email':'user@example.com','action':'updated','native_session_verified':verified},side_effect=error),
            update_token=AsyncMock(),disable_token=AsyncMock())
        request=admin.ImportTokensRequest(tokens=[admin.ImportTokenItem(auth_mode='flow',email='user@example.com',
            google_cookies=JAR,captcha_proxy_url='socks5://localhost:20001',is_active=active,video_concurrency=0)])
        with patch.object(admin,'token_manager',manager), patch.object(admin,'_normalize_plugin_captcha_proxy_url',return_value=('socks5://localhost:20001',True)):
            old=config.captcha_method
            config.set_captcha_method('native_cdp')
            try: result=await admin.import_tokens(request,token='test')
            finally: config.set_captcha_method(old)
        return result,manager

    async def test_flow_import_verifies_real_cookies_without_labs_and_preserves_disabled(self):
        result,manager=await self.import_flow(active=False)
        self.assertEqual(result['updated'],1)
        self.assertIsNone(result['errors'])
        manager.flow_client.st_to_at.assert_not_awaited()
        self.assertEqual(manager.sync_flow_session.await_args.kwargs,{'expected_email':'user@example.com','auto_enable':False})
        self.assertEqual(manager.update_token.await_args.kwargs['video_concurrency'],0)
        manager.disable_token.assert_awaited_once_with(7)

    async def test_unverified_or_protected_import_is_not_counted_as_success(self):
        for kwargs in ({'verified':False},{'error':NativeSessionError('local_session_external_sync_forbidden',protocol='angular')},
                       {'error':ValueError('upstream-private-secret')}):
            result,manager=await self.import_flow(**kwargs)
            self.assertEqual(result['updated'],0)
            self.assertTrue(result['errors'])
            manager.update_token.assert_not_awaited()
            manager.flow_client.st_to_at.assert_not_awaited()
            self.assertNotIn('secret',str(result))
