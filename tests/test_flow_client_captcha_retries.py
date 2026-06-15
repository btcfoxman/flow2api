import unittest
from unittest.mock import AsyncMock, patch

from src.core.config import config
from src.services.flow_client import FlowClient


class FlowClientCaptchaRetriesTests(unittest.TestCase):
    def setUp(self):
        self._original_method = config.captcha_method
        self._original_max_retries = config.flow_max_retries

    def tearDown(self):
        config.set_captcha_method(self._original_method)
        config.set_flow_max_retries(self._original_max_retries)

    def test_adspower_uses_recovery_retries_when_configured_as_one(self):
        config.set_captcha_method("adspower")
        config.set_flow_max_retries(1)

        self.assertEqual(FlowClient(None)._captcha_aware_max_retries(), 3)

    def test_api_captcha_keeps_configured_retry_count(self):
        config.set_captcha_method("yescaptcha")
        config.set_flow_max_retries(1)

        self.assertEqual(FlowClient(None)._captcha_aware_max_retries(), 1)

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


class FlowClientBrowserFetchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._original_method = config.captcha_method
        self._original_max_retries = config.flow_max_retries

    def tearDown(self):
        config.set_captcha_method(self._original_method)
        config.set_flow_max_retries(self._original_max_retries)

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


if __name__ == "__main__":
    unittest.main()
