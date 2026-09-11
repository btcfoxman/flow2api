"""HTTP contract for the updater's read-only, credential-safe precheck."""
import json
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import admin
from src.core.generation_errors import NativeSessionError
from src.core.models import Token


def account(**changes):
    return Token(**{**dict(id=7, email="account@example.com", st="private-st", at="private-at",
        at_expires=datetime.now(timezone.utc) + timedelta(hours=2),
        captcha_proxy_url="socks5://private-user:private-pass@localhost:20020",
        google_cookies=json.dumps([{"name":"SID","value":"private-cookie","domain":".google.com","path":"/"},
                                   {"name":"OSID","value":"private-cookie","domain":"flow.google.com","path":"/"}])), **changes})


class PluginCheckTokensTests(unittest.TestCase):
    def setUp(self):
        self.database = SimpleNamespace(
            get_plugin_config=AsyncMock(return_value=SimpleNamespace(connection_token="private-connection")),
            get_all_tokens=AsyncMock(return_value=[account()]))
        for target, value in [("db", self.database), ("token_manager", Mock(spec=[])),
                              ("config", SimpleNamespace(captcha_method="native_cdp"))]:
            p = patch.object(admin, target, value)
            p.start()
            self.addCleanup(p.stop)
        marker = patch.object(admin, "local_session_state", return_value=None)
        self.marker = marker.start()
        self.addCleanup(marker.stop)
        app = FastAPI()
        app.include_router(admin.router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def check(self, payload=None, auth="Bearer private-connection"):
        return self.client.post("/api/plugin/check-tokens", json={} if payload is None else payload,
                                headers={} if auth is None else {"Authorization": auth})

    def test_registered_route_and_allowlisted_response(self):
        response = self.check()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"success": True, "tokens": [{"email":"account@example.com",
            "is_active":True, "needs_refresh":False, "sync_allowed":True, "auth_mode":"labs"}]})
        for secret in ("private-st", "private-at", "private-cookie", "private-pass", "private-connection"):
            self.assertNotIn(secret, response.text)
        self.database.get_all_tokens.assert_awaited_once_with()

    def test_bearer_and_legacy_raw_connection_token(self):
        self.assertEqual(self.check(auth="private-connection").status_code, 200)

    def test_authentication_precedes_account_reads(self):
        for auth in (None, "Bearer incorrect", "Bearer ", "incorrect"):
            self.assertEqual(self.check(auth=auth).status_code, 401)
        self.database.get_all_tokens.assert_not_awaited()

    def test_unconfigured_connection_token_rejects_access(self):
        self.database.get_plugin_config.return_value.connection_token = ""
        self.assertEqual(self.check(auth="").status_code, 401)
        self.database.get_all_tokens.assert_not_awaited()

    def test_filter_is_case_insensitive_deduplicated_and_excludes_missing_accounts(self):
        self.database.get_all_tokens.return_value = [account(), account(id=8, email="second@example.com")]
        response = self.check({"emails":[" ACCOUNT@EXAMPLE.COM ", "account@example.com", "absent@example.com"]})
        self.assertEqual([t["email"] for t in response.json()["tokens"]], ["account@example.com"])
        self.assertEqual(self.check({"emails":["absent@example.com"]}).json()["tokens"], [])
        self.assertEqual(len(self.check({"emails":[]}).json()["tokens"]), 2)

    def test_malformed_filters_are_rejected_without_account_reads(self):
        for emails in (None, "account@example.com", {}, [1], [None], [""], ["no-at"],
                       ["a b@example.com"], ["a" * 321 + "@example.com"], ["a@example.com"] * 1001):
            self.assertEqual(self.check({"emails":emails}).status_code, 400)
        self.database.get_all_tokens.assert_not_awaited()

    def test_refresh_reasons_use_cached_state_only(self):
        for changes in ({"is_active":False}, {"at":None}, {"st":""}, {"at_expires":None},
                        {"at_expires":datetime.now(timezone.utc) - timedelta(seconds=1)},
                        {"at_expires":datetime.now(timezone.utc) + timedelta(minutes=59)},
                        {"google_cookies":None}):
            self.database.get_all_tokens.return_value = [account(**changes)]
            self.assertTrue(self.check().json()["tokens"][0]["needs_refresh"], changes)

    def test_timezone_naive_and_non_utc_expirations_and_zero_credits(self):
        for expires in (datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=2),
                        datetime.now(timezone(timedelta(hours=8))) + timedelta(hours=2)):
            self.database.get_all_tokens.return_value = [account(at_expires=expires, credits=0)]
            self.assertFalse(self.check().json()["tokens"][0]["needs_refresh"])

    def test_non_native_accounts_do_not_require_native_cookies(self):
        self.database.get_all_tokens.return_value = [account(google_cookies=None)]
        with patch.object(admin, "config", SimpleNamespace(captcha_method="yescaptcha")):
            self.assertFalse(self.check().json()["tokens"][0]["needs_refresh"])
        self.marker.assert_not_called()

    def test_independent_or_corrupt_marker_prevents_external_refresh(self):
        self.database.get_all_tokens.return_value = [account(is_active=False, at_expires=None)]
        for state in ({"version":1}, NativeSessionError("local_session_state_invalid")):
            self.marker.side_effect = state if isinstance(state, Exception) else None
            self.marker.return_value = state
            token = self.check().json()["tokens"][0]
            self.assertFalse(token["sync_allowed"])
            self.assertFalse(token["needs_refresh"])
            self.assertEqual(token["sync_block_reason"], "independent_login")
            self.assertFalse(token["is_active"])

    def test_empty_database_does_not_create_or_refresh_accounts(self):
        self.database.get_all_tokens.return_value = []
        self.assertEqual(self.check().json(), {"success":True, "tokens":[]})

    def test_native_refresh_uses_cookie_expiry_not_missing_labs_at(self):
        manager=SimpleNamespace(native_sessions=SimpleNamespace(available=lambda token:True))
        for hours,expected in ((2,False),(.5,True)):
            token=account(auth_mode="flow",at=None,at_expires=None)
            cookies=json.loads(token.google_cookies)
            for cookie in cookies:
                cookie['expires']=(datetime.now(timezone.utc)+timedelta(hours=hours)).timestamp()
            token.google_cookies=json.dumps(cookies)
            self.database.get_all_tokens.return_value=[token]
            with patch.object(admin,'token_manager',manager):
                response=self.check()
            result=response.json()['tokens'][0]
            self.assertEqual(result['needs_refresh'],expected)
            self.assertTrue(result['flow_cookie_expires_at'])
            self.assertNotIn('private-cookie',response.text)


if __name__ == "__main__":
    unittest.main()
