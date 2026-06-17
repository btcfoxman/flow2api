import time
import unittest
from unittest.mock import patch

from src.services.browser_captcha import (
    BROWSER_FETCH_BOOTSTRAP_URL,
    BrowserCaptchaService,
    TokenBrowser,
    _active_adspower_profile_payload,
    _adspower_profile_proxy_url,
    _stop_adspower_profile,
)


class _FakePlaywrightPage:
    def __init__(self, url="about:blank"):
        self.url = url
        self.closed = False
        self.goto_calls = []

    def is_closed(self):
        return self.closed

    async def goto(self, url, **kwargs):
        self.url = url
        self.goto_calls.append((url, kwargs))

    async def close(self):
        self.closed = True


class _FakePlaywrightContext:
    def __init__(self, pages=None):
        self.pages = list(pages or [])
        self.created_pages = []

    async def new_page(self):
        page = _FakePlaywrightPage()
        self.pages.append(page)
        self.created_pages.append(page)
        return page


class _FakeFetchPage(_FakePlaywrightPage):
    async def route(self, url, handler):
        self.route_url = url
        self.route_handler = handler

    async def evaluate(self, script, payload=None):
        return {"status": 200, "statusText": "OK", "text": '{"ok": true}'}


class _FakeFetchContext(_FakePlaywrightContext):
    async def new_page(self):
        page = _FakeFetchPage()
        self.pages.append(page)
        self.created_pages.append(page)
        return page


class _FailingNewPageContext:
    pages = []

    async def new_page(self):
        raise RuntimeError("BrowserContext.new_page: Target page, context or browser has been closed")


class _FakeServiceBrowser:
    def __init__(self, token="recaptcha-token", request_ref=None):
        self.token = token
        self.request_ref = request_ref
        self.get_token_calls = []
        self.recycle_calls = []
        self.finish_calls = []

    async def get_token(self, project_id, website_key, action, token_proxy_url=None):
        self.get_token_calls.append((project_id, website_key, action, token_proxy_url))
        return self.token, self.request_ref

    async def recycle_browser(self, reason="unknown", rotate_profile=True):
        self.recycle_calls.append((reason, rotate_profile))

    async def notify_generation_request_finished(self, request_ref=None):
        self.finish_calls.append(request_ref)


class AdsPowerProfileProxyTests(unittest.TestCase):
    def test_resolves_profile_proxy_from_v1_user_list(self):
        payload = {
            "code": 0,
            "data": {
                "list": [
                    {
                        "user_id": "kwrxc3b",
                        "user_proxy_config": {
                            "proxy_type": "socks5",
                            "proxy_host": "127.0.0.1",
                            "proxy_port": "20021",
                            "proxy_user": "user name",
                            "proxy_password": "p@ss",
                        },
                    }
                ]
            },
        }

        with patch("src.services.browser_captcha._adspower_request_json", return_value=payload):
            self.assertEqual(
                _adspower_profile_proxy_url("kwrxc3b"),
                "socks5://user%20name:p%40ss@127.0.0.1:20021",
            )

    def test_rewrites_loopback_host_when_adspower_api_is_remote(self):
        payload = {
            "code": 0,
            "data": {
                "list": [
                    {
                        "profile_id": "profile-1",
                        "user_proxy_config": {
                            "proxy_type": "http",
                            "proxy_host": "127.0.0.1",
                            "proxy_port": "8080",
                        },
                    }
                ]
            },
        }

        with patch("src.services.browser_captcha._adspower_request_json", return_value=payload), patch(
            "src.services.browser_captcha._adspower_api_host_for_cdp",
            return_value="adspower-cli",
        ):
            self.assertEqual(
                _adspower_profile_proxy_url("profile-1"),
                "http://adspower-cli:8080",
            )

    def test_stop_profile_tries_legacy_stop_params(self):
        calls = []

        def fake_request(method, path, params=None, body=None):
            calls.append((method, path, params, body))
            if params == {"id": "kwrxc3b"}:
                return {"code": 0}
            return {"code": 1, "msg": "not found"}

        with patch("src.services.browser_captcha._adspower_request_json", side_effect=fake_request):
            self.assertTrue(_stop_adspower_profile("kwrxc3b"))

        self.assertEqual(
            calls,
            [
                ("GET", "/api/v1/browser/stop", {"user_id": "kwrxc3b"}, None),
                ("GET", "/api/v1/browser/stop", {"id": "kwrxc3b"}, None),
            ],
        )

    def test_active_profile_payload_requires_cdp_endpoint(self):
        payload = {
            "code": 0,
            "data": {
                "status": "Active",
                "debug_port": "9993",
            },
        }

        with patch("src.services.browser_captcha._adspower_request_json", return_value=payload), patch(
            "src.services.browser_captcha._adspower_debug_ws_from_port",
            return_value="ws://127.0.0.1:9993/devtools/browser/test",
        ):
            self.assertIs(_active_adspower_profile_payload("kwrxc3b"), payload)

    def test_start_profile_reuses_active_profile_without_start_call(self):
        calls = []

        def fake_request(method, path, params=None, body=None):
            calls.append((method, path, params, body))
            if path == "/api/v1/browser/active":
                return {
                    "code": 0,
                    "data": {
                        "status": "Active",
                        "debug_port": "9993",
                    },
                }
            raise AssertionError(f"unexpected start call: {method} {path}")

        browser = TokenBrowser(token_id=0, user_data_dir="tmp/unit-adspower")
        with patch("src.services.browser_captcha._adspower_request_json", side_effect=fake_request), patch(
            "src.services.browser_captcha._adspower_profile_id_for_slot",
            return_value="kwrxc3b",
        ), patch(
            "src.services.browser_captcha._adspower_debug_ws_from_port",
            return_value="ws://127.0.0.1:9993/devtools/browser/test",
        ):
            self.assertEqual(browser._start_adspower_profile()["data"]["status"], "Active")

        self.assertEqual(
            calls,
            [("GET", "/api/v1/browser/active", {"user_id": "kwrxc3b"}, None)],
        )


class AdsPowerBlankPageCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_keepalive_adopts_existing_blank_page_and_closes_extras(self):
        keepalive = _FakePlaywrightPage("about:blank")
        surplus_blank = _FakePlaywrightPage("about:blank")
        real_page = _FakePlaywrightPage("https://labs.google/fx/tools/flow")
        context = _FakePlaywrightContext([keepalive, surplus_blank, real_page])
        browser = TokenBrowser(token_id=0, user_data_dir="tmp/unit-adspower")
        browser._shared_context = context

        adopted = await browser._ensure_shared_keepalive_page()

        self.assertIs(adopted, keepalive)
        self.assertIs(browser._shared_keepalive_page, keepalive)
        self.assertEqual(context.created_pages, [])
        self.assertFalse(keepalive.closed)
        self.assertTrue(surplus_blank.closed)
        self.assertFalse(real_page.closed)

    async def test_blank_cleanup_keeps_tracked_keepalive_only(self):
        keepalive = _FakePlaywrightPage("about:blank")
        surplus_blank = _FakePlaywrightPage("about:blank")
        new_tab = _FakePlaywrightPage("chrome://newtab/")
        real_page = _FakePlaywrightPage("https://example.com")
        context = _FakePlaywrightContext([keepalive, surplus_blank, new_tab, real_page])
        browser = TokenBrowser(token_id=0, user_data_dir="tmp/unit-adspower")
        browser._shared_context = context
        browser._shared_keepalive_page = keepalive

        closed_count = await browser._cleanup_blank_pages(
            context,
            keep_page=keepalive,
            reason="unit_test",
        )

        self.assertEqual(closed_count, 2)
        self.assertFalse(keepalive.closed)
        self.assertTrue(surplus_blank.closed)
        self.assertTrue(new_tab.closed)
        self.assertFalse(real_page.closed)

    async def test_cleanup_closes_auth_provider_bootstrap_pages(self):
        keepalive = _FakePlaywrightPage("about:blank")
        auth_provider = _FakePlaywrightPage(BROWSER_FETCH_BOOTSTRAP_URL)
        auth_provider_query = _FakePlaywrightPage(f"{BROWSER_FETCH_BOOTSTRAP_URL}?x=1")
        flow_page = _FakePlaywrightPage("https://labs.google/fx/tools/flow")
        context = _FakePlaywrightContext([keepalive, auth_provider, auth_provider_query, flow_page])
        browser = TokenBrowser(token_id=0, user_data_dir="tmp/unit-adspower")
        browser._shared_context = context
        browser._shared_keepalive_page = keepalive

        closed_count = await browser._cleanup_blank_pages(
            context,
            keep_page=keepalive,
            reason="unit_test_auth_provider",
        )

        self.assertEqual(closed_count, 2)
        self.assertFalse(keepalive.closed)
        self.assertTrue(auth_provider.closed)
        self.assertTrue(auth_provider_query.closed)
        self.assertFalse(flow_page.closed)

    async def test_cleanup_keeps_inflight_browser_fetch_bootstrap_page(self):
        keepalive = _FakePlaywrightPage("about:blank")
        inflight_fetch = _FakePlaywrightPage(BROWSER_FETCH_BOOTSTRAP_URL)
        stale_fetch = _FakePlaywrightPage(BROWSER_FETCH_BOOTSTRAP_URL)
        context = _FakePlaywrightContext([keepalive, inflight_fetch, stale_fetch])
        browser = TokenBrowser(token_id=0, user_data_dir="tmp/unit-adspower")
        browser._shared_context = context
        browser._shared_keepalive_page = keepalive
        browser._protected_temporary_page_ids.add(id(inflight_fetch))

        closed_count = await browser._cleanup_blank_pages(
            context,
            keep_page=keepalive,
            reason="unit_test_inflight_fetch",
        )

        self.assertEqual(closed_count, 1)
        self.assertFalse(keepalive.closed)
        self.assertFalse(inflight_fetch.closed)
        self.assertTrue(stale_fetch.closed)

    async def test_keepalive_creates_one_blank_page_when_context_has_no_blank(self):
        real_page = _FakePlaywrightPage("https://example.com")
        context = _FakePlaywrightContext([real_page])
        browser = TokenBrowser(token_id=0, user_data_dir="tmp/unit-adspower")
        browser._shared_context = context

        keepalive = await browser._ensure_shared_keepalive_page()

        self.assertIs(keepalive, context.created_pages[0])
        self.assertEqual(keepalive.url, "about:blank")
        self.assertFalse(keepalive.closed)
        self.assertFalse(real_page.closed)


class BrowserFetchLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_browser_fetch_retries_once_when_context_was_closed(self):
        browser = TokenBrowser(token_id=0, user_data_dir="tmp/unit-browser-fetch")
        contexts = [_FailingNewPageContext(), _FakeFetchContext()]
        recycle_reasons = []
        busy_seen = []

        async def fake_get_or_create_shared_browser():
            busy_seen.append(browser.is_busy())
            return None, None, contexts.pop(0)

        async def fake_recycle_browser(reason="unknown", rotate_profile=True):
            recycle_reasons.append((reason, rotate_profile))

        async def fake_capture_page_fingerprint(page):
            return None

        browser._get_or_create_shared_browser = fake_get_or_create_shared_browser
        browser.recycle_browser = fake_recycle_browser
        browser._capture_page_fingerprint = fake_capture_page_fingerprint

        result = await browser.fetch_json(
            url="https://aisandbox-pa.googleapis.com/v1/video:poll",
            json_data={"operation": "op-1"},
            timeout=10,
        )

        self.assertEqual(result, {"ok": True})
        self.assertEqual(recycle_reasons, [("browser_fetch_context_closed", False)])
        self.assertEqual(busy_seen, [True, True])
        self.assertFalse(browser.is_busy())

    async def test_browser_fetch_recycles_on_terminal_context_closed(self):
        browser = TokenBrowser(token_id=0, user_data_dir="tmp/unit-browser-fetch-terminal")
        contexts = [_FailingNewPageContext(), _FailingNewPageContext()]
        recycle_reasons = []

        async def fake_get_or_create_shared_browser():
            return None, None, contexts.pop(0)

        async def fake_recycle_browser(reason="unknown", rotate_profile=True):
            recycle_reasons.append((reason, rotate_profile))

        browser._get_or_create_shared_browser = fake_get_or_create_shared_browser
        browser.recycle_browser = fake_recycle_browser

        with self.assertRaisesRegex(RuntimeError, "Target page, context or browser has been closed"):
            await browser.fetch_json(
                url="https://aisandbox-pa.googleapis.com/v1/video:submit",
                json_data={"requests": []},
                timeout=10,
            )

        self.assertEqual(
            recycle_reasons,
            [
                ("browser_fetch_context_closed", False),
                ("browser_fetch_context_closed", False),
            ],
        )
        self.assertFalse(browser.is_busy())


class TokenBrowserProfileRestartTests(unittest.IsolatedAsyncioTestCase):
    async def test_restarts_adspower_profile_after_repeated_empty_tokens(self):
        browser = TokenBrowser(token_id=0, user_data_dir="tmp/unit-adspower-restart")

        async def fake_get_or_create_shared_browser(token_proxy_url=None):
            return None, None, object()

        async def fake_execute_captcha(context, project_id, website_key, action):
            return None

        async def fast_sleep(_delay):
            return None

        browser._get_or_create_shared_browser = fake_get_or_create_shared_browser
        browser._execute_captcha = fake_execute_captcha

        with patch("src.services.browser_captcha._is_adspower_enabled", return_value=True), patch(
            "src.services.browser_captcha._adspower_profile_id_for_slot",
            return_value="kwrxc3b",
        ), patch("src.services.browser_captcha._stop_adspower_profile", return_value=True) as stop_profile, patch(
            "src.services.browser_captcha.asyncio.sleep",
            new=fast_sleep,
        ):
            token, request_ref = await browser.get_token(
                project_id="project-1",
                website_key="site-key",
                action="VIDEO_GENERATION",
            )

        self.assertIsNone(token)
        self.assertIsNone(request_ref)
        stop_profile.assert_called_once_with("kwrxc3b")
        self.assertEqual(browser._consecutive_token_failures, 0)


class BrowserCaptchaServiceRuntimeClosedTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_token_binds_successful_slot_until_request_finished(self):
        service = BrowserCaptchaService(db=None)
        service._check_available = lambda: None
        fake_browser = _FakeServiceBrowser(token="recaptcha-token")
        service._browsers[0] = fake_browser

        token, browser_ref = await service.get_token("project-1", action="VIDEO_GENERATION")

        self.assertEqual(token, "recaptcha-token")
        self.assertIsInstance(browser_ref, str)
        self.assertTrue(browser_ref.startswith("0:"))
        request_ref = browser_ref.split(":", 1)[1]
        self.assertIn(request_ref, service._bound_request_slots)
        self.assertEqual(service._slot_reservations, {})
        self.assertTrue(service._is_slot_busy_for_allocation(0))

        await service.report_request_finished(browser_ref)

        self.assertNotIn(request_ref, service._bound_request_slots)
        self.assertFalse(service._is_slot_busy_for_allocation(0))
        self.assertEqual(fake_browser.finish_calls, [request_ref])

    async def test_get_token_skips_slot_bound_to_active_request(self):
        service = BrowserCaptchaService(db=None)
        service._check_available = lambda: None
        service._browser_count = 2
        fake_browser_0 = _FakeServiceBrowser(token="token-0")
        fake_browser_1 = _FakeServiceBrowser(token="token-1")
        service._browsers[0] = fake_browser_0
        service._browsers[1] = fake_browser_1

        token_0, browser_ref_0 = await service.get_token("project-1", action="VIDEO_GENERATION")
        service._round_robin_index = 0
        token_1, browser_ref_1 = await service.get_token("project-2", action="VIDEO_GENERATION")

        self.assertEqual(token_0, "token-0")
        self.assertEqual(token_1, "token-1")
        self.assertTrue(str(browser_ref_0).startswith("0:"))
        self.assertTrue(str(browser_ref_1).startswith("1:"))

    async def test_select_browser_waits_instead_of_reusing_bound_slot(self):
        service = BrowserCaptchaService(db=None)
        service._browser_count = 1
        service._bound_request_slots["active-ref"] = {
            "browser_id": 0,
            "started_at": time.time(),
        }

        sleep_calls = []

        async def release_after_sleep(_delay):
            sleep_calls.append(_delay)
            service._bound_request_slots.clear()
            return None

        with patch(
            "src.services.browser_captcha.asyncio.sleep",
            new=release_after_sleep,
        ):
            browser_id = await service._select_browser_id("project-1")

        self.assertEqual(browser_id, 0)
        self.assertEqual(len(sleep_calls), 1)
        self.assertEqual(service._slot_reservations, {0: 1})

    async def test_get_token_recycles_slot_when_token_missing(self):
        service = BrowserCaptchaService(db=None)
        service._check_available = lambda: None
        fake_browser = _FakeServiceBrowser(token=None)
        service._browsers[0] = fake_browser

        token, browser_ref = await service.get_token("project-1", action="VIDEO_GENERATION")

        self.assertIsNone(token)
        self.assertEqual(browser_ref, 0)
        self.assertEqual(
            fake_browser.get_token_calls,
            [("project-1", service.website_key, "VIDEO_GENERATION", None)],
        )
        self.assertEqual(fake_browser.recycle_calls, [("token_missing_after_attempts", False)])
        self.assertEqual(service._slot_reservations, {})

    async def test_report_error_recycles_runtime_closed_without_profile_rotation(self):
        service = BrowserCaptchaService(db=None)
        fake_browser = _FakeServiceBrowser()
        service._browsers[0] = fake_browser

        await service.report_error(0, error_reason="browser runtime closed")

        self.assertEqual(fake_browser.recycle_calls, [("browser runtime closed", False)])
