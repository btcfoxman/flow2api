import unittest
import asyncio
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from src.api.admin import plugin_update_token
from src.services.proxy_manager import ProxyManager
from src.services.flow_client import FlowClient
from src.core.config import config


class PluginTokenProxySyncTests(unittest.IsolatedAsyncioTestCase):
    def _dependencies(self, *, existing=True):
        token = (
            SimpleNamespace(
                id=7,
                email="profile@example.com",
                is_active=True,
                captcha_proxy_url="socks5://127.0.0.1:20019",
            )
            if existing
            else None
        )
        database = SimpleNamespace(
            get_plugin_config=AsyncMock(
                return_value=SimpleNamespace(
                    connection_token="connection-secret",
                    auto_enable_on_update=True,
                )
            ),
            get_token_by_email=AsyncMock(return_value=token),
            get_token_by_st=AsyncMock(return_value=token),
            get_token=AsyncMock(return_value=token),
        )
        manager = SimpleNamespace(
            flow_client=SimpleNamespace(
                st_to_at=AsyncMock(
                    return_value={
                        "access_token": "access-token",
                        "expires": "2099-08-28T00:00:00Z",
                        "user": {"email": "profile@example.com"},
                    }
                ),
                get_credits=AsyncMock(return_value={"credits":100,"userPaygateTier":"PAYGATE_TIER_ONE"}),
                credential_proxy_context=lambda proxy: nullcontext(),
                native_account_proxy_context=lambda token_id: nullcontext(),
            ),
            update_token=AsyncMock(),
            add_token=AsyncMock(
                return_value=SimpleNamespace(id=8, email="profile@example.com", is_active=True)
            ),
            enable_token=AsyncMock(),
        )
        return database, manager

    async def _call(self, request, *, existing=True):
        database, manager = self._dependencies(existing=existing)
        with patch("src.api.admin.db", database), patch(
            "src.api.admin.token_manager", manager
        ), patch("src.api.admin.proxy_manager", ProxyManager(database)):
            response = await plugin_update_token(
                request,
                authorization="Bearer connection-secret",
            )
        return response, manager

    async def test_existing_token_updates_profile_proxy(self):
        response, manager = await self._call(
            {
                "session_token": "session-token",
                "captcha_proxy_url": "socks5://127.0.0.1:20020",
            }
        )

        self.assertTrue(response["proxy_updated"])
        self.assertTrue(response["proxy_configured"])
        self.assertEqual(
            manager.update_token.await_args.kwargs["captcha_proxy_url"],
            "socks5://127.0.0.1:20020",
        )

    async def test_old_extension_omission_preserves_existing_proxy(self):
        response, manager = await self._call({"session_token": "session-token"})

        self.assertFalse(response["proxy_updated"])
        self.assertTrue(response["proxy_configured"])
        self.assertNotIn(
            "captcha_proxy_url",
            manager.update_token.await_args.kwargs,
        )

    async def test_new_token_receives_profile_proxy(self):
        response, manager = await self._call(
            {
                "session_token": "session-token",
                "captcha_proxy_url": "127.0.0.1:20020",
            },
            existing=False,
        )

        self.assertEqual(response["action"], "added")
        self.assertTrue(response["proxy_configured"])
        self.assertEqual(
            manager.add_token.await_args.kwargs["captcha_proxy_url"],
            "http://127.0.0.1:20020",
        )

    async def test_explicit_empty_proxy_is_rejected(self):
        with self.assertRaises(HTTPException) as caught:
            await self._call(
                {
                    "session_token": "session-token",
                    "captcha_proxy_url": "",
                }
            )

        self.assertEqual(caught.exception.status_code, 400)

    async def test_invalid_proxy_scheme_is_rejected(self):
        with self.assertRaises(HTTPException) as caught:
            await self._call(
                {
                    "session_token": "session-token",
                    "captcha_proxy_url": "ftp://127.0.0.1:20020",
                }
            )

        self.assertEqual(caught.exception.status_code, 400)

    async def _checked_call(self, database, manager, request=None):
        with patch('src.api.admin.db', database), patch('src.api.admin.token_manager', manager), \
             patch('src.api.admin.proxy_manager', ProxyManager(database)):
            return await plugin_update_token(request or {'session_token':'session-token'}, 'Bearer connection-secret')

    async def test_expired_response_is_not_saved_or_auto_enabled(self):
        database, manager = self._dependencies()
        database.get_token_by_email.return_value.is_active = False
        manager.flow_client.st_to_at.return_value['expires'] = '2000-01-01T00:00:00Z'
        with self.assertRaises(HTTPException) as caught:
            await self._checked_call(database, manager)
        self.assertEqual(caught.exception.status_code,400)
        manager.update_token.assert_not_awaited()
        manager.enable_token.assert_not_awaited()
        manager.flow_client.get_credits.assert_not_awaited()

    async def test_revoked_or_unverifiable_at_never_updates_account(self):
        for error, status in [(RuntimeError('HTTP 401 secret-at'),400), (TimeoutError('secret-route'),503)]:
            database, manager = self._dependencies()
            manager.flow_client.get_credits.side_effect=error
            with self.assertRaises(HTTPException) as caught:
                await self._checked_call(database, manager)
            self.assertEqual(caught.exception.status_code,status)
            self.assertNotIn('secret-',caught.exception.detail)
            manager.update_token.assert_not_awaited()
            manager.enable_token.assert_not_awaited()

    async def test_missing_or_malformed_expiration_is_not_saved(self):
        for expires in [None,'','not-a-date']:
            database,manager=self._dependencies()
            manager.flow_client.st_to_at.return_value['expires']=expires
            with self.assertRaises(HTTPException) as caught:
                await self._checked_call(database,manager)
            self.assertEqual(caught.exception.status_code,400)
            manager.update_token.assert_not_awaited()
            manager.enable_token.assert_not_awaited()

    async def test_verified_sync_refreshes_balance_and_respects_auto_enable_setting(self):
        for auto_enable in [True,False]:
            database, manager = self._dependencies()
            database.get_token_by_email.return_value.is_active=False
            database.get_plugin_config.return_value.auto_enable_on_update=auto_enable
            response=await self._checked_call(database, manager)
            self.assertEqual(response['account_active'],auto_enable)
            self.assertTrue(response['oauth_verified'])
            self.assertEqual(manager.update_token.await_args.kwargs['credits'],100)
            self.assertEqual(manager.enable_token.await_count,int(auto_enable))

    async def test_entire_sync_uses_explicit_proxy_and_restores_context(self):
        database, manager = self._dependencies(existing=False)
        client=FlowClient(ProxyManager(database),db=database)
        client._set_request_fingerprint({'proxy_url':'socks5://previous:1'})
        async def exchange(st):
            self.assertEqual(client.get_request_fingerprint(),{'proxy_url':'socks5://account-route:1234'})
            return {'access_token':'valid-at','expires':'2099-01-01T00:00:00Z','user':{'email':'profile@example.com'}}
        async def balance(at):
            self.assertEqual(client.get_request_fingerprint(),{'proxy_url':'socks5://account-route:1234'})
            return {'credits':100}
        async def add(**kwargs):
            self.assertEqual(client.get_request_fingerprint(),{'proxy_url':'socks5://account-route:1234'})
            return SimpleNamespace(id=8,email='profile@example.com',is_active=True)
        client.st_to_at=exchange
        client.get_credits=balance
        manager.flow_client=client
        manager.add_token=AsyncMock(side_effect=add)
        response=await self._checked_call(database,manager,{'session_token':'session-token','captcha_proxy_url':'socks5://account-route:1234'})
        self.assertTrue(response['oauth_verified'])
        self.assertEqual(client.get_request_fingerprint(),{'proxy_url':'socks5://previous:1'})

    async def test_native_omission_uses_known_account_route_or_requires_explicit_proxy(self):
        previous=config.captcha_method
        config.set_captcha_method('native_cdp')
        self.addCleanup(config.set_captcha_method,previous)
        database,manager=self._dependencies()
        client=FlowClient(None,db=database)
        async def exchange(st):
            self.assertEqual(client.get_request_fingerprint(),{'proxy_url':'socks5://127.0.0.1:20019'})
            return {'access_token':'at','expires':'2099-01-01T00:00:00Z','user':{'email':'profile@example.com'}}
        client.st_to_at=AsyncMock(side_effect=exchange)
        client.get_credits=AsyncMock(return_value={'credits':100})
        manager.flow_client=client
        with patch('src.api.admin.local_session_state',return_value=None):
            await self._checked_call(database,manager)
        client.st_to_at.reset_mock()
        database.get_token_by_st.return_value=None
        with self.assertRaises(HTTPException) as caught:
            await self._checked_call(database,manager)
        self.assertEqual(caught.exception.status_code,400)
        client.st_to_at.assert_not_awaited()

    async def test_parallel_proxy_contexts_are_isolated_even_on_failure(self):
        client=FlowClient(None)
        async def check(proxy):
            try:
                async with client.credential_proxy_context(proxy):
                    await asyncio.sleep(0)
                    self.assertEqual(client.get_request_fingerprint(),{'proxy_url':proxy})
                    raise RuntimeError('test')
            except RuntimeError:
                pass
            self.assertIsNone(client.get_request_fingerprint())
        await asyncio.gather(check('socks5://one:1'),check('socks5://two:2'))


if __name__ == "__main__":
    unittest.main()
