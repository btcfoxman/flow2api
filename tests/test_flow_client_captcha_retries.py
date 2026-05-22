import unittest

from src.core.config import config
from src.services.flow_client import FlowClient


class FlowClientCaptchaRetriesTests(unittest.TestCase):
    def setUp(self):
        self._original_method = config.captcha_method
        self._original_max_retries = config.flow_max_retries

    def tearDown(self):
        config.set_captcha_method(self._original_method)
        config.set_flow_max_retries(self._original_max_retries)

    def test_adspower_uses_recovery_retry_when_configured_as_one(self):
        config.set_captcha_method("adspower")
        config.set_flow_max_retries(1)

        self.assertEqual(FlowClient(None)._captcha_aware_max_retries(), 2)

    def test_api_captcha_keeps_configured_retry_count(self):
        config.set_captcha_method("yescaptcha")
        config.set_flow_max_retries(1)

        self.assertEqual(FlowClient(None)._captcha_aware_max_retries(), 1)


if __name__ == "__main__":
    unittest.main()
