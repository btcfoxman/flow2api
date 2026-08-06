import unittest
import base64
from io import BytesIO
from unittest.mock import AsyncMock, patch

from PIL import Image

from src.services.flow_client import FlowClient


JPEG_BYTES = b"\xff\xd8\xff" + b"0" * 16


class FlowClientUploadImageTests(unittest.IsolatedAsyncioTestCase):
    async def test_project_scoped_upload_uses_new_endpoint_with_project_id(self):
        client = FlowClient(proxy_manager=None)

        request_calls = []

        async def fake_make_request(**kwargs):
            request_calls.append(kwargs)
            return {
                "media": {
                    "name": "new-media-id",
                }
            }

        client._make_request = AsyncMock(side_effect=fake_make_request)

        media_id = await client.upload_image(
            at="test-at",
            image_bytes=JPEG_BYTES,
            aspect_ratio="IMAGE_ASPECT_RATIO_LANDSCAPE",
            project_id="project-123",
        )

        self.assertEqual(media_id, "new-media-id")
        self.assertEqual(len(request_calls), 1)
        self.assertTrue(request_calls[0]["url"].endswith("/flow/uploadImage"))
        self.assertEqual(
            request_calls[0]["json_data"]["clientContext"]["projectId"],
            "project-123",
        )
        self.assertTrue(request_calls[0]["json_data"]["clientContext"]["sessionId"])
        self.assertNotIn("headers", request_calls[0])
        self.assertNotIn("apply_default_client_headers", request_calls[0])

    async def test_project_scoped_upload_accepts_media_list_response(self):
        client = FlowClient(proxy_manager=None)

        request_calls = []

        async def fake_make_request(**kwargs):
            request_calls.append(kwargs)
            return {
                "media": [
                    {
                        "name": "new-media-id",
                        "projectId": "project-123",
                    }
                ]
            }

        client._make_request = AsyncMock(side_effect=fake_make_request)

        media_id = await client.upload_image(
            at="test-at",
            image_bytes=JPEG_BYTES,
            aspect_ratio="IMAGE_ASPECT_RATIO_LANDSCAPE",
            project_id="project-123",
        )

        self.assertEqual(media_id, "new-media-id")
        self.assertEqual(len(request_calls), 1)
        self.assertTrue(request_calls[0]["url"].endswith("/flow/uploadImage"))

    async def test_project_scoped_upload_does_not_fallback_to_legacy_endpoint(self):
        client = FlowClient(proxy_manager=None)

        request_calls = []

        async def fake_make_request(**kwargs):
            request_calls.append(kwargs)
            if kwargs["url"].endswith("/flow/uploadImage"):
                raise RuntimeError("HTTP 500: upstream failed")
            self.fail("带 project_id 的上传不应回退到 legacy 接口")

        client._make_request = AsyncMock(side_effect=fake_make_request)

        with patch("builtins.print") as mock_print, self.assertRaisesRegex(
            RuntimeError,
            "legacy :uploadUserImage fallback is disabled",
        ) as ctx:
            await client.upload_image(
                at="test-at",
                image_bytes=JPEG_BYTES,
                aspect_ratio="IMAGE_ASPECT_RATIO_LANDSCAPE",
                project_id="project-123",
            )

        error_message = str(ctx.exception)
        self.assertIn("HTTP 500: upstream failed", error_message)
        self.assertIn("mime=image/jpeg", error_message)
        self.assertIn(f"bytes={len(JPEG_BYTES)}", error_message)
        self.assertTrue(
            any(
                "[UPLOAD] /flow/uploadImage failed project_id=project-123" in str(call.args[0])
                for call in mock_print.call_args_list
            )
        )
        self.assertEqual(len(request_calls), 1)
        self.assertEqual(
            request_calls[0]["json_data"]["clientContext"]["projectId"],
            "project-123",
        )

    async def test_project_scoped_upload_normalizes_image_after_invalid_argument(self):
        client = FlowClient(proxy_manager=None)

        request_calls = []
        normalized_bytes = b"\xff\xd8\xffnormalized"

        async def fake_make_request(**kwargs):
            request_calls.append(kwargs)
            if len(request_calls) == 1:
                raise RuntimeError(
                    "Flow API request failed: HTTP Error 400: Request contains an invalid argument."
                )
            return {
                "media": {
                    "name": "normalized-media-id",
                }
            }

        client._make_request = AsyncMock(side_effect=fake_make_request)
        client._convert_to_jpeg = lambda image_bytes: normalized_bytes  # type: ignore[method-assign]

        media_id = await client.upload_image(
            at="test-at",
            image_bytes=JPEG_BYTES,
            aspect_ratio="IMAGE_ASPECT_RATIO_LANDSCAPE",
            project_id="project-123",
        )

        self.assertEqual(media_id, "normalized-media-id")
        self.assertEqual(len(request_calls), 2)
        self.assertTrue(request_calls[1]["url"].endswith("/flow/uploadImage"))
        self.assertEqual(request_calls[1]["json_data"]["mimeType"], "image/jpeg")
        self.assertEqual(
            request_calls[1]["json_data"]["imageBytes"],
            base64.b64encode(normalized_bytes).decode("utf-8"),
        )
        self.assertTrue(request_calls[1]["json_data"]["clientContext"]["sessionId"])

    async def test_rejected_image_normalization_bounds_dimensions_and_uses_rgb_jpeg(self):
        client = FlowClient(proxy_manager=None)
        source = BytesIO()
        Image.new("RGBA", (5000, 100), (255, 0, 0, 128)).save(source, format="PNG")

        normalized = client._convert_to_jpeg(source.getvalue())

        with Image.open(BytesIO(normalized)) as image:
            self.assertEqual(image.format, "JPEG")
            self.assertEqual(image.mode, "RGB")
            self.assertLessEqual(max(image.size), 4096)

    async def test_upload_without_project_id_keeps_legacy_fallback(self):
        client = FlowClient(proxy_manager=None)

        request_calls = []

        async def fake_make_request(**kwargs):
            request_calls.append(kwargs)
            if kwargs["url"].endswith("/flow/uploadImage"):
                raise RuntimeError("HTTP 500: upstream failed")
            if kwargs["url"].endswith(":uploadUserImage"):
                return {
                    "mediaGenerationId": {
                        "mediaGenerationId": "legacy-media-id",
                    }
                }
            self.fail(f"Unexpected url: {kwargs['url']}")

        client._make_request = AsyncMock(side_effect=fake_make_request)

        media_id = await client.upload_image(
            at="test-at",
            image_bytes=JPEG_BYTES,
            aspect_ratio="IMAGE_ASPECT_RATIO_LANDSCAPE",
            project_id=None,
        )

        self.assertEqual(media_id, "legacy-media-id")
        self.assertEqual(len(request_calls), 2)
        self.assertNotIn(
            "projectId",
            request_calls[1]["json_data"]["clientContext"],
        )


if __name__ == "__main__":
    unittest.main()
