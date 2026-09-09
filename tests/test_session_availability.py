import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.core.session_availability import SessionAvailability
from src.services.load_balancer import LoadBalancer


def token(token_id=1, **changes):
    values = dict(id=token_id, st="st", google_cookies="cookies", captcha_proxy_url="same-proxy",
                  at="at", credits=100, user_paygate_tier="PAYGATE_TIER_ONE", image_enabled=True,
                  video_enabled=True, email="test@example.invalid")
    values.update(changes)
    return SimpleNamespace(**values)


class SessionAvailabilityTests(unittest.TestCase):
    def test_imported_about_page_requires_new_credentials_not_timer_expiry(self):
        from src.core.generation_errors import NativeSessionError
        state = SessionAvailability()
        error = NativeSessionError("project_context_unavailable", page_url="https://flow.google.com/about")
        with patch("src.core.native_session_state.local_session_state", return_value=None):
            state.reject(token(), error)
        with patch("src.core.session_availability.time.monotonic", return_value=10**12):
            self.assertFalse(state.available(token()))
            self.assertTrue(state.available(token(google_cookies="new-snapshot")))

    def test_independent_local_login_still_allows_bounded_health_recheck(self):
        from src.core.generation_errors import NativeSessionError
        state = SessionAvailability()
        with patch("src.core.native_session_state.local_session_state", return_value={"version":1}), \
             patch("src.core.session_availability.time.monotonic", return_value=100):
            state.reject(token(), NativeSessionError("flow_login_unavailable"))
        with patch("src.core.session_availability.time.monotonic", return_value=401):
            self.assertTrue(state.available(token()))

    def test_rejected_session_is_account_local_and_keeps_no_credentials(self):
        state = SessionAvailability()
        state.reject(token())
        self.assertFalse(state.available(token()))
        self.assertTrue(state.available(token(2)))
        self.assertNotIn("cookies", repr(state._blocked))

    def test_new_session_or_proxy_is_immediately_eligible(self):
        for field in ("st", "google_cookies", "captcha_proxy_url"):
            state = SessionAvailability()
            state.reject(token())
            self.assertTrue(state.available(token(**{field: "replacement"})))

    def test_oauth_and_balance_refresh_do_not_bypass_session_backoff(self):
        state = SessionAvailability()
        state.reject(token())
        self.assertFalse(state.available(token(at="new-at", credits=200)))

    def test_expiry_allows_bounded_recheck_and_delete_forgets_state(self):
        state = SessionAvailability()
        with patch("src.core.session_availability.time.monotonic", return_value=100):
            state.reject(token())
        with patch("src.core.session_availability.time.monotonic", return_value=399):
            self.assertFalse(state.available(token()))
        with patch("src.core.session_availability.time.monotonic", return_value=400):
            self.assertTrue(state.available(token()))
        state.reject(token())
        state.discard(1)
        self.assertTrue(state.available(token()))


class SessionSchedulingTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_snapshot_is_not_sent_to_browser_after_restart(self):
        for raw in (None, "", "[]"):
            manager = SimpleNamespace(native_sessions=SessionAvailability(), get_active_tokens=AsyncMock(return_value=[token(google_cookies=raw)]))
            balancer = LoadBalancer(manager)
            with patch('src.services.load_balancer.config', SimpleNamespace(captcha_method='native_cdp')), \
                 patch('src.services.load_balancer.local_session_state', return_value=None):
                self.assertIsNone(await balancer.select_token(for_video_generation=True, minimum_credits=4, track_pending=True))
            self.assertFalse(balancer._pending_credits)

    async def test_incomplete_import_is_skipped_without_consuming_slots(self):
        manager = SimpleNamespace(native_sessions=SessionAvailability(), get_active_tokens=AsyncMock(return_value=[token()]))
        balancer = LoadBalancer(manager)
        with patch('src.services.load_balancer.config', SimpleNamespace(captcha_method='native_cdp')), \
             patch('src.services.load_balancer.local_session_state',return_value=None):
            selected=await balancer.select_token(for_image_generation=True,minimum_credits=4,track_pending=True)
        self.assertIsNone(selected)
        self.assertFalse(balancer._pending_credits)

    async def test_incomplete_external_snapshot_does_not_block_independent_profile(self):
        manager = SimpleNamespace(native_sessions=SessionAvailability(), get_active_tokens=AsyncMock(return_value=[token()]),
                                  needs_at_refresh=lambda value: False, ensure_valid_token=AsyncMock(side_effect=lambda value:value))
        balancer = LoadBalancer(manager)
        balancer._check_extension_route=AsyncMock(return_value=(True,''))
        with patch('src.services.load_balancer.config', SimpleNamespace(captcha_method='native_cdp',call_logic_mode='random')), \
             patch('src.services.load_balancer.local_session_state',return_value={'version':1}):
            selected=await balancer.select_token(for_image_generation=True,minimum_credits=4)
        self.assertEqual(selected.id,1)

    async def test_known_invalid_session_is_skipped_before_reserving_credits(self):
        state = SessionAvailability()
        state.reject(token())
        manager = SimpleNamespace(native_sessions=state, get_active_tokens=AsyncMock(return_value=[token()]))
        balancer = LoadBalancer(manager)
        with patch("src.services.load_balancer.config", SimpleNamespace(captcha_method="native_cdp")):
            for kind in ("image", "video"):
                selected = await balancer.select_token(**{"for_" + kind + "_generation": True},
                                                       track_pending=True, minimum_credits=4)
                self.assertIsNone(selected)
        self.assertFalse(balancer._pending_credits)
        self.assertFalse(balancer._video_proxy_pending)
