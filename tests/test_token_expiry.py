import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.api import admin
from src.core.flow_cookies import flow_cookie_expiry, normalize_google_cookies


FUTURE = 4102444800
PAST = 946684800
JAR = [
    {"name": "SID", "value": "test-root-secret", "domain": ".google.com", "path": "/", "expires": FUTURE},
    {"name": "OSID", "value": "test-flow-secret", "domain": "flow.google.com", "path": "/", "expires": FUTURE + 3600},
    {"name": "__Secure-OSID", "value": "test-secure-secret", "domain": "flow.google.com", "path": "/", "expires": FUTURE + 3601},
]


class FlowCookieExpiryTests(unittest.TestCase):
    def test_earliest_login_credential_not_analytics_or_wrong_scope(self):
        unrelated = [
            {**JAR[0], "name": "NID", "expires": PAST},
            {**JAR[0], "domain": "accounts.google.com", "expires": PAST},
            {**JAR[0], "path": "/other", "expires": PAST},
            {**JAR[1], "domain": "google.com", "expires": PAST},
        ]
        result = flow_cookie_expiry(JAR + unrelated)
        self.assertEqual(result["flow_cookie_expires_at"], "2100-01-01T00:00:00+00:00")
        self.assertEqual(result["flow_cookie_expiry_status"], "known")
        self.assertFalse(result["flow_cookie_has_session_cookies"])
        self.assertNotIn("secret", json.dumps(result))

    def test_flow_cookie_can_expire_before_google_sid(self):
        result = flow_cookie_expiry([JAR[0], {**JAR[1], "expires": FUTURE - 3600}])
        self.assertEqual(result["flow_cookie_expires_at"], "2099-12-31T23:00:00+00:00")

    def test_expired_snapshot_is_displayed_but_not_imported(self):
        expired = [{**c, "expires": PAST} for c in JAR]
        self.assertEqual(flow_cookie_expiry(expired)["flow_cookie_expiry_status"], "expired")
        self.assertEqual(flow_cookie_expiry(expired)["flow_cookie_expires_at"], "2000-01-01T00:00:00+00:00")
        with self.assertRaises(ValueError):
            normalize_google_cookies(expired)
        mixed = [{**JAR[0], "expires": PAST}, JAR[1]]
        self.assertEqual(flow_cookie_expiry(mixed)["flow_cookie_expiry_status"], "expired")
        self.assertEqual(len(json.loads(normalize_google_cookies(mixed))), 1)

    def test_session_cookies_have_no_invented_expiry(self):
        for expires in (None, -1):
            with self.subTest(expires=expires):
                result = flow_cookie_expiry([{**c, "expires": expires} for c in JAR])
                self.assertIsNone(result["flow_cookie_expires_at"])
                self.assertEqual(result["flow_cookie_expiry_status"], "session")
                self.assertTrue(result["flow_cookie_has_session_cookies"])

    def test_mixed_session_and_persistent_credentials_are_flagged(self):
        result = flow_cookie_expiry([{**JAR[0], "expires": None}, JAR[1]])
        self.assertEqual(result["flow_cookie_expiry_status"], "known")
        self.assertTrue(result["flow_cookie_has_session_cookies"])

    def test_missing_required_credentials_are_not_given_an_expiry(self):
        for raw in ([JAR[0]], [JAR[1]], [{**JAR[0], "value": ""}, JAR[1]],
                    [{**JAR[0], "path": "/other"}, JAR[1]]):
            with self.subTest(raw=raw):
                result = flow_cookie_expiry(raw)
                self.assertEqual(result["flow_cookie_expiry_status"], "incomplete")
                self.assertIsNone(result["flow_cookie_expires_at"])

    def test_missing_invalid_and_out_of_range_metadata_fail_safely(self):
        self.assertEqual(flow_cookie_expiry(None)["flow_cookie_expiry_status"], "unavailable")
        for raw in ("not-json", [{**c, "expires": float("inf")} for c in JAR],
                    [{**c, "expires": 1e100} for c in JAR], [{**JAR[0], "domain": "evil.invalid"}]):
            with self.subTest(raw=raw):
                result = flow_cookie_expiry(raw)
                self.assertEqual(result["flow_cookie_expiry_status"], "invalid")
                self.assertIsNone(result["flow_cookie_expires_at"])

    def test_extension_expiration_date_and_json_string_are_supported(self):
        jar = [{k: v for k, v in c.items() if k != "expires"} | {"expirationDate": c["expires"]} for c in JAR]
        self.assertEqual(flow_cookie_expiry(json.dumps(jar)), flow_cookie_expiry(JAR))

    def test_epoch_zero_is_expired_not_session_only(self):
        result = flow_cookie_expiry([{**cookie, "expires": 0} for cookie in JAR])
        self.assertEqual(result["flow_cookie_expiry_status"], "expired")
        self.assertEqual(result["flow_cookie_expires_at"], "1970-01-01T00:00:00+00:00")


class AdminExpiryTests(unittest.IsolatedAsyncioTestCase):
    async def test_token_list_adds_new_fields_without_repurposing_legacy_at(self):
        rows = [
            {"id": 1, "auth_mode": "flow", "google_cookies": json.dumps(JAR), "at_expires": None},
            {"id": 2, "auth_mode": "labs", "google_cookies": json.dumps(JAR), "at_expires": "2099-01-01T00:00:00Z"},
        ]
        fake = SimpleNamespace(get_all_tokens_with_stats=AsyncMock(return_value=rows))
        with patch.object(admin, "db", fake):
            result = await admin.get_tokens(token="test-admin")
        self.assertIsNone(result[0]["at_expires"])
        self.assertEqual(result[0]["flow_cookie_expires_at"], "2100-01-01T00:00:00+00:00")
        self.assertEqual(result[1]["at_expires"], rows[1]["at_expires"])
        self.assertIsNone(result[1]["flow_cookie_expires_at"])
        self.assertNotIn("secret", json.dumps(result))
        self.assertNotIn("google_cookies", result[0])

    async def test_refresh_returns_expiry_metadata_for_flow_and_preserves_labs(self):
        for mode in ("flow", "labs"):
            with self.subTest(mode=mode):
                current = SimpleNamespace(id=1, email="test@example.com", auth_mode=mode,
                    google_cookies=json.dumps(JAR), at_expires=None if mode == "flow" else datetime(2099, 1, 1, tzinfo=timezone.utc))
                manager = SimpleNamespace(_refresh_at=AsyncMock(return_value=True), get_token=AsyncMock(return_value=current))
                with patch.object(admin, "token_manager", manager):
                    result = await admin.refresh_at(1, token="test-admin")
                self.assertTrue(result["success"])
                self.assertEqual(result["token"]["auth_mode"], mode)
                self.assertEqual(bool(result["token"]["flow_cookie_expires_at"]), mode == "flow")
                self.assertNotIn("secret", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
