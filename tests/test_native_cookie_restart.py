import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.services.browser_captcha_native_cdp import NativeCdpAccountBrowser


class CookieRestartTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_credentials_recheck_legacy_redirect_but_unchanged_do_not(self):
        with tempfile.TemporaryDirectory() as directory:
            token = SimpleNamespace(st="old", google_cookies=None)
            worker = NativeCdpAccountBrowser(1, SimpleNamespace(get_token=AsyncMock(return_value=token)))
            worker.profile_dir = Path(directory)
            worker._legacy_migrated = True
            worker._legacy_auth_revision = hashlib.sha256(b"old\0").hexdigest()
            worker._create_page_session = AsyncMock(return_value=("target", "session"))
            worker._seed_session_cookie = AsyncMock()
            worker._open_real_project_page = AsyncMock()
            worker._capture_fingerprint = AsyncMock()
            await worker._get_or_create_project_session("p", "labs")
            worker._open_real_project_page.assert_awaited_with("session", "p", "angular")
            token.st = "new"
            await worker._get_or_create_project_session("p", "labs")
            worker._open_real_project_page.assert_awaited_with("session", "p", "labs")

    def worker(self, directory, cookies, google=""):
        worker = NativeCdpAccountBrowser(1, SimpleNamespace(get_token=AsyncMock(
            return_value=SimpleNamespace(st="stored-st", google_cookies=google))))
        worker.profile_dir = Path(directory)
        signature = hashlib.sha256(("stored-st\0" + google).encode()).hexdigest()
        (worker.profile_dir / ".flow2api-cookie-seed").write_text(signature)
        (worker.profile_dir / ".flow2api-google-cookie-seed").write_text(hashlib.sha256(google.encode()).hexdigest())
        worker.connection = SimpleNamespace(send=AsyncMock(return_value={"cookies": cookies, "success": True}))
        return worker

    async def test_restart_restores_missing_session_cookie_despite_seed_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            worker = self.worker(directory, [])
            self.assertTrue(await worker._seed_session_cookie("s"))
            calls = [c for c in worker.connection.send.await_args_list if c.args[0] == "Network.setCookie"]
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0].args[1]["value"], "stored-st")

    async def test_live_rotated_session_is_not_replaced_by_unchanged_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            worker = self.worker(directory, [{"name": "__Secure-next-auth.session-token", "value": "rotated"}])
            await worker._seed_session_cookie("s")
            self.assertFalse(await worker._seed_session_cookie("s"))
            self.assertFalse(any(c.args[0] == "Network.setCookie" for c in worker.connection.send.await_args_list))

    async def test_missing_labs_cookie_never_replays_rotated_google_cookie_seed(self):
        google = json.dumps([{"name":"SID","value":"old-google","domain":".google.com"},
                             {"name":"OSID","value":"old-flow","domain":"flow.google.com"}])
        with tempfile.TemporaryDirectory() as directory:
            worker = self.worker(directory, [], google)
            await worker._seed_session_cookie("s", "angular")
            names = [c.args[1]["name"] for c in worker.connection.send.await_args_list if c.args[0] == "Network.setCookie"]
            self.assertEqual(names, ["__Secure-next-auth.session-token"])
