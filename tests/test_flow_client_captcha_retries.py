import unittest
from unittest.mock import AsyncMock, patch

from src.core.config import config
from src.services.flow_client import FlowClient


class FlowClientCaptchaRetriesTests(unittest.TestCase):
    def setUp(self):
        self._original_method = config.captcha_method
        self._original_max_retries = config.flow_max_retries
        self._original_captcha_max_retries = config.captcha_max_retries

    def tearDown(self):
        config.set_captcha_method(self._original_method)
        config.set_flow_max_retries(self._original_max_retries)
        config.set_captcha_max_retries(self._original_captcha_max_retries)

    def test_adspower_uses_captcha_retry_budget(self):
        config.set_captcha_method("adspower")
        config.set_flow_max_retries(1)
        config.set_captcha_max_retries(5)

        self.assertEqual(FlowClient(None)._captcha_aware_max_retries(), 5)

    def test_api_captcha_uses_captcha_retry_budget(self):
        config.set_captcha_method("yescaptcha")
        config.set_flow_max_retries(1)
        config.set_captcha_max_retries(4)

        self.assertEqual(FlowClient(None)._captcha_aware_max_retries(), 4)

    def test_native_cdp_uses_captcha_retry_budget(self):
        config.set_captcha_method("native_cdp")
        config.set_flow_max_retries(1)
        config.set_captcha_max_retries(6)

        self.assertEqual(FlowClient(None)._captcha_aware_max_retries(), 6)

    def test_non_captcha_method_keeps_flow_retry_budget(self):
        config.set_captcha_method("extension")
        config.set_flow_max_retries(2)
        config.set_captcha_max_retries(5)

        self.assertEqual(FlowClient(None)._captcha_aware_max_retries(), 2)

    def test_browser_runtime_closed_error_is_retryable(self):
        reason = FlowClient(None)._get_retry_reason(
            "Flow browser API request failed: Page.evaluate: "
            "Target page, context or browser has been closed"
        )

        self.assertEqual(reason, "browser runtime closed")

    def test_quota_reached_error_is_not_retryable(self):
        reason = FlowClient(None)._get_retry_reason(
            "PUBLIC_ERROR_USER_QUOTA_REACHED: Resource has been exhausted (e.g. check quota)."
        )

        self.assertIsNone(reason)

    def test_browser_failed_to_fetch_error_is_retryable(self):
        reason = FlowClient(None)._get_retry_reason(
            "Flow browser API request failed: browser fetch failed: TypeError: Failed to fetch"
        )

        self.assertEqual(reason, "\u7f51\u7edc/TLS\u9519\u8bef")


class FlowClientBrowserFetchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._original_method = config.captcha_method
        self._original_max_retries = config.flow_max_retries
        self._original_captcha_max_retries = config.captcha_max_retries

    def tearDown(self):
        config.set_captcha_method(self._original_method)
        config.set_flow_max_retries(self._original_max_retries)
        config.set_captcha_max_retries(self._original_captcha_max_retries)

    async def test_adspower_token_binds_browser_ref_to_fingerprint(self):
        config.set_captcha_method("adspower")
        client = FlowClient(None)

        class FakeBrowserCaptchaService:
            async def get_token(self, project_id, action, token_id=None):
                return "recaptcha-token", 0

            async def get_fingerprint(self, browser_ref):
                return {
                    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/135.0.0.0",
                    "sec_ch_ua_platform": '"Windows"',
                }

        fake_service = FakeBrowserCaptchaService()
        with patch(
            "src.services.browser_captcha.BrowserCaptchaService.get_instance",
            AsyncMock(return_value=fake_service),
        ):
            token, browser_ref = await client._get_recaptcha_token(
                "project-1",
                action="VIDEO_GENERATION",
                token_id=123,
            )

        self.assertEqual(token, "recaptcha-token")
        self.assertEqual(browser_ref, 0)
        fingerprint = client.get_request_fingerprint()
        self.assertEqual(fingerprint["browser_ref"], 0)
        self.assertEqual(fingerprint["sec_ch_ua_platform"], '"Windows"')

    async def test_video_api_uses_bound_browser_fetch_for_adspower(self):
        config.set_captcha_method("adspower")
        client = FlowClient(None)
        client._set_request_fingerprint({"browser_ref": 0})

        class FakeBrowserCaptchaService:
            def __init__(self):
                self.call = None

            async def fetch_json(self, **kwargs):
                self.call = kwargs
                return {"operations": [{"operation": {"name": "task-1"}}]}

        fake_service = FakeBrowserCaptchaService()
        with patch(
            "src.services.browser_captcha.BrowserCaptchaService.get_instance",
            AsyncMock(return_value=fake_service),
        ):
            result = await client._make_video_api_request(
                url="https://aisandbox-pa.googleapis.com/v1/video:batchAsyncGenerateVideoReferenceImages",
                json_data={"requests": []},
                at="access-token",
                timeout=30,
            )

        self.assertEqual(result["operations"][0]["operation"]["name"], "task-1")
        self.assertEqual(fake_service.call["browser_ref"], 0)
        self.assertEqual(fake_service.call["method"], "POST")
        self.assertEqual(fake_service.call["headers"]["authorization"], "Bearer access-token")
        self.assertEqual(fake_service.call["headers"]["content-type"], "text/plain;charset=UTF-8")

    async def test_browser_fetch_transport_failure_falls_back_without_empty_fingerprint_proxy(self):
        config.set_captcha_method("adspower")
        client = FlowClient(None)
        client._set_request_fingerprint({"browser_ref": 0, "proxy_url": None})

        class FakeBrowserCaptchaService:
            async def fetch_json(self, **kwargs):
                raise RuntimeError("browser fetch failed: TypeError: Failed to fetch")

        fake_service = FakeBrowserCaptchaService()
        fallback_result = {"operations": [{"operation": {"name": "task-1"}}]}

        with patch(
            "src.services.browser_captcha.BrowserCaptchaService.get_instance",
            AsyncMock(return_value=fake_service),
        ), patch.object(
            client,
            "_make_request",
            AsyncMock(return_value=fallback_result),
        ) as make_request:
            result = await client._make_video_api_request(
                url="https://aisandbox-pa.googleapis.com/v1/video:batchAsyncGenerateVideoReferenceImages",
                json_data={"requests": []},
                at="access-token",
                timeout=30,
            )

        self.assertEqual(result, fallback_result)
        self.assertFalse(make_request.await_args.kwargs["respect_fingerprint_proxy"])
        self.assertFalse(make_request.await_args.kwargs["allow_urllib_fallback"])

    async def test_stale_browser_ref_falls_back_and_clears_ref(self):
        config.set_captcha_method("adspower")
        client = FlowClient(None)
        client._set_request_fingerprint({
            "browser_ref": "0:released-ref",
            "proxy_url": "socks5://xray:20005",
            "user_agent": "Mozilla/5.0 test",
        })

        class FakeBrowserCaptchaService:
            async def fetch_json(self, **kwargs):
                raise RuntimeError(
                    "browser_ref is no longer bound to an active generation request"
                )

        fallback_result = {"operations": [{"operation": {"name": "task-1"}}]}

        with patch(
            "src.services.browser_captcha.BrowserCaptchaService.get_instance",
            AsyncMock(return_value=FakeBrowserCaptchaService()),
        ), patch.object(
            client,
            "_make_request",
            AsyncMock(return_value=fallback_result),
        ) as make_request:
            result = await client._make_video_api_request(
                url="https://aisandbox-pa.googleapis.com/v1/video:batchCheckAsyncVideoGenerationStatus",
                json_data={"operations": []},
                at="access-token",
                timeout=30,
            )

        self.assertEqual(result, fallback_result)
        fingerprint = client.get_request_fingerprint()
        self.assertNotIn("browser_ref", fingerprint)
        self.assertEqual(fingerprint["proxy_url"], "socks5://xray:20005")
        self.assertFalse(make_request.await_args.kwargs["allow_urllib_fallback"])

    def test_browser_fetch_transport_error_allows_http_fallback(self):
        client = FlowClient(None)

        self.assertTrue(
            client._should_fallback_browser_video_request(
                "browser fetch failed: TypeError: Failed to fetch"
            )
        )

    def test_stale_browser_ref_error_allows_http_fallback(self):
        client = FlowClient(None)

        self.assertTrue(
            client._should_fallback_browser_video_request(
                "browser_ref is no longer bound to an active generation request"
            )
        )

    def test_recaptcha_evaluation_error_does_not_allow_http_fallback(self):
        client = FlowClient(None)

        self.assertFalse(
            client._should_fallback_browser_video_request(
                "PUBLIC_ERROR_UNUSUAL_ACTIVITY: reCAPTCHA evaluation failed"
            )
        )
        self.assertFalse(
            client._should_fallback_browser_video_request(
                "PUBLIC_ERROR_UNUSUAL_ACTIVITY_TOO_MUCH_TRAFFIC: reCAPTCHA evaluation failed"
            )
        )

    async def test_missing_recaptcha_token_retries_until_configured_max_attempts(self):
        config.set_captcha_method("adspower")
        config.set_captcha_max_retries(4)
        client = FlowClient(None)
        client._acquire_video_launch_gate = AsyncMock(return_value=(True, 0, 0))
        client._release_video_launch_gate = AsyncMock()
        client._get_recaptcha_token = AsyncMock(
            side_effect=[
                (None, None),
                (None, None),
                ("recaptcha-token", "0:request-ref"),
            ]
        )
        client._make_video_api_request = AsyncMock(
            return_value={"operations": [{"operation": {"name": "task-1"}}]}
        )
        client._notify_browser_captcha_error = AsyncMock()
        client._notify_browser_captcha_request_finished = AsyncMock()

        result = await client.generate_video_text(
            at="access-token",
            project_id="project-1",
            prompt="text only",
            model_key="abra_t2v_10s",
            aspect_ratio="VIDEO_ASPECT_RATIO_LANDSCAPE",
            token_id=123,
        )

        self.assertEqual(result["operations"][0]["operation"]["name"], "task-1")
        self.assertEqual(client._get_recaptcha_token.await_count, 3)
        self.assertEqual(client._make_video_api_request.await_count, 1)
        self.assertEqual(client._notify_browser_captcha_error.await_count, 2)

    async def test_missing_recaptcha_token_fails_after_configured_max_attempts(self):
        config.set_captcha_method("adspower")
        config.set_captcha_max_retries(4)
        client = FlowClient(None)
        client._acquire_video_launch_gate = AsyncMock(return_value=(True, 0, 0))
        client._release_video_launch_gate = AsyncMock()
        client._get_recaptcha_token = AsyncMock(return_value=(None, None))
        client._notify_browser_captcha_error = AsyncMock()

        with self.assertRaisesRegex(Exception, "Failed to obtain reCAPTCHA token"):
            await client.generate_video_text(
                at="access-token",
                project_id="project-1",
                prompt="text only",
                model_key="abra_t2v_10s",
                aspect_ratio="VIDEO_ASPECT_RATIO_LANDSCAPE",
                token_id=123,
            )

        self.assertEqual(client._get_recaptcha_token.await_count, 4)
        self.assertEqual(client._notify_browser_captcha_error.await_count, 4)

    async def test_browser_fetch_failure_retries_video_submit_with_new_recaptcha_token(self):
        config.set_captcha_method("adspower")
        config.set_captcha_max_retries(4)
        client = FlowClient(None)
        client._acquire_video_launch_gate = AsyncMock(return_value=(True, 0, 0))
        client._release_video_launch_gate = AsyncMock()
        client._get_recaptcha_token = AsyncMock(
            side_effect=[
                ("recaptcha-token-1", "0:request-ref-1"),
                ("recaptcha-token-2", "0:request-ref-2"),
            ]
        )
        client._make_video_api_request = AsyncMock(
            side_effect=[
                Exception("Flow browser API request failed: browser fetch failed: TypeError: Failed to fetch"),
                {"operations": [{"operation": {"name": "task-2"}}]},
            ]
        )
        client._notify_browser_captcha_error = AsyncMock()
        client._notify_browser_captcha_request_finished = AsyncMock()

        result = await client.generate_video_text(
            at="access-token",
            project_id="project-1",
            prompt="text only",
            model_key="abra_t2v_10s",
            aspect_ratio="VIDEO_ASPECT_RATIO_LANDSCAPE",
            token_id=123,
        )

        self.assertEqual(result["operations"][0]["operation"]["name"], "task-2")
        self.assertEqual(client._get_recaptcha_token.await_count, 2)
        self.assertEqual(client._make_video_api_request.await_count, 2)
        self.assertEqual(client._notify_browser_captcha_error.await_count, 1)
        self.assertEqual(client._notify_browser_captcha_request_finished.await_count, 2)

    async def test_unusual_activity_stops_after_trying_two_profiles(self):
        config.set_captcha_method("adspower")
        config.set_captcha_max_retries(5)
        client = FlowClient(None)
        client._notify_browser_captcha_error = AsyncMock()

        error = Exception(
            "PUBLIC_ERROR_UNUSUAL_ACTIVITY: reCAPTCHA evaluation failed"
        )
        with patch("src.services.flow_client.asyncio.sleep", AsyncMock()):
            should_retry_first = await client._handle_retryable_generation_error(
                error=error,
                retry_attempt=0,
                max_retries=5,
                browser_id="0:request-1",
                project_id="project-1",
                log_prefix="[VIDEO]",
            )
            should_retry_second = await client._handle_retryable_generation_error(
                error=error,
                retry_attempt=1,
                max_retries=5,
                browser_id="1:request-2",
                project_id="project-1",
                log_prefix="[VIDEO]",
            )

        self.assertTrue(should_retry_first)
        self.assertFalse(should_retry_second)
        self.assertEqual(client._notify_browser_captcha_error.await_count, 2)


if __name__ == "__main__":
    unittest.main()
