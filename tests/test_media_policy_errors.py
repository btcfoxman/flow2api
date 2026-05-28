import unittest

from src.core.media_errors import (
    is_media_policy_error,
    media_generation_failure_response,
)
from src.services.flow_client import FlowClient


class MediaPolicyErrorTests(unittest.TestCase):
    def test_image_policy_failure_uses_client_error_status(self):
        message, status_code = media_generation_failure_response(
            "image",
            "Flow API request failed: PUBLIC_ERROR_UNSAFE_GENERATION: Request contains an invalid argument.",
        )

        self.assertEqual(status_code, 400)
        self.assertIn("\u5185\u5bb9\u5b89\u5168\u7b56\u7565\u62d2\u7edd", message)
        self.assertNotIn("PUBLIC_ERROR_UNSAFE_GENERATION", message)

    def test_prominent_people_filter_is_policy_error(self):
        self.assertTrue(
            is_media_policy_error("PUBLIC_ERROR_PROMINENT_PEOPLE_FILTER_FAILED")
        )

    def test_flow_client_does_not_retry_media_policy_error(self):
        reason = FlowClient(None)._get_retry_reason(
            "PUBLIC_ERROR_UNSAFE_GENERATION: Request contains an invalid argument."
        )

        self.assertIsNone(reason)

    def test_flow_client_still_retries_generic_public_error(self):
        reason = FlowClient(None)._get_retry_reason("PUBLIC_ERROR_INTERNAL")

        self.assertIsNotNone(reason)


if __name__ == "__main__":
    unittest.main()
