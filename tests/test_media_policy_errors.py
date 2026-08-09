import unittest

from src.core.media_errors import (
    is_media_traffic_error,
    is_media_policy_error,
    is_project_image_upload_error,
    is_project_image_upload_invalid_argument_error,
    media_generation_failure_reason,
    media_generation_failure_response,
    project_image_upload_failure_response,
    sanitize_public_error_message,
    MEDIA_POLICY_REASON_PROMINENT_PEOPLE,
    MEDIA_TRAFFIC_REASON,
    VIDEO_UPLOAD_FAILURE_MESSAGE,
    VIDEO_UPLOAD_INVALID_ARGUMENT_MESSAGE,
)
from src.services.flow_client import FlowClient
from src.services.generation_handler import GenerationHandler


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
        error = "PUBLIC_ERROR_PROMINENT_PEOPLE_FILTER_FAILED"

        self.assertTrue(is_media_policy_error(error))
        self.assertEqual(
            media_generation_failure_reason(error),
            MEDIA_POLICY_REASON_PROMINENT_PEOPLE,
        )
        message, status_code = media_generation_failure_response("video", error)
        self.assertEqual(status_code, 400)
        self.assertIn("\u4eba\u7269/\u516c\u4f17\u4eba\u7269\u8fc7\u6ee4", message)
        self.assertIn("\u4e25\u683c\u9501\u8138", message)
        self.assertNotIn("PROMINENT_PEOPLE", message)

    def test_unusual_activity_is_reported_as_traffic_control(self):
        error = (
            "PUBLIC_ERROR_UNUSUAL_ACTIVITY_TOO_MUCH_TRAFFIC: "
            "reCAPTCHA evaluation failed"
        )

        self.assertFalse(is_media_policy_error(error))
        self.assertTrue(is_media_traffic_error(error))
        self.assertEqual(
            media_generation_failure_reason(error),
            MEDIA_TRAFFIC_REASON,
        )
        message, status_code = media_generation_failure_response("video", error)
        self.assertEqual(status_code, 429)
        self.assertIn("\u6d41\u91cf\u6216\u5f02\u5e38\u6d3b\u52a8\u98ce\u63a7", message)
        self.assertIn("\u4e0d\u662f\u5185\u5bb9\u5b89\u5168\u62d2\u7edd", message)

    def test_generic_invalid_argument_is_not_policy_error(self):
        self.assertFalse(
            is_media_policy_error("HTTP Error 400: Request contains an invalid argument.")
        )

    def test_project_image_upload_invalid_argument_is_separate_error(self):
        self.assertTrue(
            is_project_image_upload_invalid_argument_error(
                "Project-scoped image upload failed via /flow/uploadImage "
                "(project_id=project-123, cause=Flow API request failed: "
                "HTTP Error 400: Request contains an invalid argument.)"
            )
        )

        handler = GenerationHandler.__new__(GenerationHandler)
        self.assertEqual(
            handler._normalize_error_message(
                "Project-scoped image upload failed via /flow/uploadImage "
                "(project_id=project-123, cause=Flow API request failed: "
                "HTTP Error 400: Request contains an invalid argument.)"
            ),
            VIDEO_UPLOAD_INVALID_ARGUMENT_MESSAGE,
        )

    def test_generic_project_image_upload_failure_is_safe_upstream_error(self):
        error = (
            "Project-scoped image upload failed via /flow/uploadImage "
            "(project_id=project-123, cause=HTTP Error 500: upstream failed)"
        )

        self.assertTrue(is_project_image_upload_error(error))
        self.assertFalse(is_project_image_upload_invalid_argument_error(error))
        message, status_code = project_image_upload_failure_response(error)
        self.assertEqual(status_code, 502)
        self.assertEqual(message, VIDEO_UPLOAD_FAILURE_MESSAGE)
        self.assertNotIn("project-123", message)

        handler = GenerationHandler.__new__(GenerationHandler)
        self.assertEqual(handler._normalize_error_message(error), message)

    def test_flow_client_does_not_retry_media_policy_error(self):
        reason = FlowClient(None)._get_retry_reason(
            "PUBLIC_ERROR_UNSAFE_GENERATION: Request contains an invalid argument."
        )

        self.assertIsNone(reason)

    def test_flow_client_still_retries_generic_public_error(self):
        reason = FlowClient(None)._get_retry_reason("PUBLIC_ERROR_INTERNAL")

        self.assertIsNotNone(reason)

    def test_task_level_failures_do_not_count_as_token_errors(self):
        handler = GenerationHandler.__new__(GenerationHandler)

        self.assertFalse(
            handler._should_record_token_error("PUBLIC_ERROR_UNSAFE_GENERATION", 400)
        )
        self.assertFalse(
            handler._should_record_token_error(
                "Project-scoped image upload failed via /flow/uploadImage "
                "(project_id=project-123, cause=HTTP Error 500)",
                500,
            )
        )
        self.assertTrue(
            handler._should_record_token_error("Token AT invalid or refresh failed", 503)
        )

    def test_public_error_message_hides_provider_terms_and_endpoints(self):
        message = sanitize_public_error_message(
            "Flow API request failed via /flow/uploadImage: upstream unavailable"
        )

        self.assertNotIn("flow", message.lower())
        self.assertNotIn("/flow/", message.lower())
        self.assertNotIn("upstream", message.lower())
        self.assertNotIn("上游", message)
        self.assertIn("生成服务", message)

        handler = GenerationHandler.__new__(GenerationHandler)
        response = handler._create_error_response(
            "生成失败: Flow browser API request failed",
            status_code=502,
        )
        self.assertNotIn("flow", response.lower())


if __name__ == "__main__":
    unittest.main()
