import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from src.api.admin import plugin_update_token
from src.services.proxy_manager import ProxyManager


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
        )
        manager = SimpleNamespace(
            flow_client=SimpleNamespace(
                st_to_at=AsyncMock(
                    return_value={
                        "access_token": "access-token",
                        "expires": "2026-08-28T00:00:00Z",
                        "user": {"email": "profile@example.com"},
                    }
                )
            ),
            update_token=AsyncMock(),
            add_token=AsyncMock(
                return_value=SimpleNamespace(id=8, email="profile@example.com")
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


if __name__ == "__main__":
    unittest.main()
