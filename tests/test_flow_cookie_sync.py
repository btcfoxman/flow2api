import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from src.api.admin import plugin_update_token
from src.core.database import Database
from src.core.models import Token
from src.core.flow_cookies import normalize_google_cookies, google_cookie_status
from src.services.browser_captcha_native_cdp import NativeCdpAccountBrowser


JAR = [{"name": "SID", "value": "test-google", "domain": ".google.com", "path": "/"},
       {"name": "__Secure-OSID", "value": "test-flow", "domain": "flow.google.com", "path": "/", "sameSite": "no_restriction"}]


class CookieTests(unittest.TestCase):
    def test_preserves_scope_and_expiry(self):
        cookies = json.loads(normalize_google_cookies(JAR + [{**JAR[0], "value": "new", "expirationDate": 4102444800}]))
        self.assertEqual(len(cookies), 2)
        self.assertEqual(cookies[0]["value"], "new")
        self.assertEqual(cookies[0]["expires"], 4102444800)
        self.assertEqual(cookies[1]["domain"], "flow.google.com")
        self.assertEqual(cookies[1]["sameSite"], "None")
        self.assertTrue(google_cookie_status(cookies)["flow_cookies_configured"])

    def test_rejects_bad_scope_unscoped_headers_and_partition_loss(self):
        for raw in ["SID=secret", [], [{**JAR[0], "domain": "google.com.evil.test"}],
                    [{**JAR[0], "value": "value\r\nInjected"}], [{**JAR[0], "expires": float("nan")}],
                    [{**JAR[0], "partitionKey": {"topLevelSite": "https://flow.google.com"}}]]:
            with self.subTest(raw_type=type(raw).__name__), self.assertRaises(ValueError):
                normalize_google_cookies(raw)

    def test_sensitive_values_excluded_from_model_serialization(self):
        token = Token(st="st", email="test@example.com", google_cookies=normalize_google_cookies(JAR))
        self.assertNotIn("google_cookies", token.model_dump())
        self.assertNotIn("test-google", repr(token))


class CookieIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_rotation_keeps_authenticated_profile_credentials(self):
        browser = NativeCdpAccountBrowser(1, SimpleNamespace(get_token=AsyncMock(return_value=SimpleNamespace(google_cookies="configured"))))
        browser.profile_solve_count = 10
        browser.stop = AsyncMock()
        browser.start = AsyncMock()
        browser._reset_profile = AsyncMock()
        await browser._prepare_profile(for_solve=True)
        browser._reset_profile.assert_not_awaited()
        browser.stop.assert_awaited_once()
        browser.start.assert_awaited_once()
        self.assertEqual(browser.profile_solve_count, 0)

    async def test_synchronized_cookies_are_not_treated_as_authenticated_page(self):
        browser = NativeCdpAccountBrowser(1, None)
        browser._requires_flow_login = True
        browser.connection = SimpleNamespace(send=AsyncMock(return_value={}))
        browser._wait_for_document_ready = AsyncMock()
        browser._evaluate = AsyncMock(side_effect=["https://flow.google.com/project/p", False])
        with self.assertRaisesRegex(RuntimeError, "complete login"):
            await browser._open_real_project_page("s", "p")

    async def test_database_roundtrip_and_update(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Database(db_path=str(Path(directory) / "test.db"))
            await db.init_db()
            async with db._connect(write=True) as conn:
                await conn.execute("ALTER TABLE tokens DROP COLUMN google_cookies")
                await conn.commit()
            await db.check_and_migrate_db()
            value = normalize_google_cookies(JAR)
            token_id = await db.add_token(Token(st="test-st", email="test@example.com", google_cookies=value))
            self.assertEqual((await db.get_token(token_id)).google_cookies, value)
            await db.update_token(token_id, google_cookies=None)
            self.assertIsNone((await db.get_token(token_id)).google_cookies)

    async def test_api_preserves_omitted_cookie_and_syncs_supplied_jar(self):
        db = SimpleNamespace(get_plugin_config=AsyncMock(return_value=SimpleNamespace(connection_token="test-key", auto_enable_on_update=False)),
            get_token_by_email=AsyncMock(return_value=SimpleNamespace(id=1, google_cookies=normalize_google_cookies(JAR), captcha_proxy_url="socks5://localhost:20001", is_active=True)))
        manager = SimpleNamespace(flow_client=SimpleNamespace(st_to_at=AsyncMock(return_value={"access_token":"at", "user":{"email":"test@example.com"}})), update_token=AsyncMock())
        with patch("src.api.admin.db", db), patch("src.api.admin.token_manager", manager):
            old = await plugin_update_token({"session_token":"st"}, "Bearer test-key")
            self.assertFalse(old["cookies_updated"])
            self.assertTrue(old["flow_cookies_configured"])
            self.assertNotIn("google_cookies", manager.update_token.await_args.kwargs)
            new = await plugin_update_token({"session_token":"st", "google_cookies":JAR}, "Bearer test-key")
            self.assertTrue(new["cookies_updated"])
            self.assertNotIn("test-google", json.dumps(new))
            self.assertEqual(manager.update_token.await_args.kwargs["google_cookies"], normalize_google_cookies(JAR))
            manager.flow_client.st_to_at.reset_mock()
            with self.assertRaises(HTTPException):
                await plugin_update_token({"session_token":"st", "google_cookies":"SID=unscoped"}, "Bearer test-key")
            manager.flow_client.st_to_at.assert_not_awaited()

    async def test_native_seeds_once_preserves_host_only_and_reloads_new_snapshot(self):
        token = SimpleNamespace(st="x" * 4000, google_cookies=normalize_google_cookies(JAR))
        db = SimpleNamespace(get_token=AsyncMock(return_value=token))
        connection = SimpleNamespace(send=AsyncMock(return_value={"success":True}))
        with tempfile.TemporaryDirectory() as directory, patch("src.services.browser_captcha_native_cdp._profile_root", return_value=Path(directory)):
            worker = NativeCdpAccountBrowser(1, db)
            worker.connection = connection
            self.assertTrue(await worker._seed_session_cookie("page-1"))
            sets = [c.args[1] for c in connection.send.await_args_list if c.args[0] == "Network.setCookie"]
            self.assertEqual(len(sets), 4)
            flow = next(c for c in sets if c["name"] == "__Secure-OSID")
            self.assertEqual(flow["url"], "https://flow.google.com/")
            self.assertNotIn("domain", flow)
            connection.send.reset_mock()
            self.assertFalse(await worker._seed_session_cookie("page-1"))
            connection.send.assert_not_awaited()
            token.st = "new-legacy-session"
            await worker._seed_session_cookie("page-1")
            self.assertEqual([c.args[1]["name"] for c in connection.send.await_args_list if c.args[0] == "Network.setCookie"], ["__Secure-next-auth.session-token"])
            connection.send.reset_mock()
            restarted = NativeCdpAccountBrowser(1, db)
            restarted.connection = connection
            await restarted._seed_session_cookie("page-2")
            connection.send.assert_not_awaited()
            token.google_cookies = normalize_google_cookies([{**c, "value":"rotated"} for c in JAR])
            self.assertTrue(await worker._seed_session_cookie("page-1"))
            self.assertGreater(connection.send.await_count, 0)
