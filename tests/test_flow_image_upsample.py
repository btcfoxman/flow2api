import base64
import io
import unittest
from unittest.mock import AsyncMock, patch

from PIL import Image

from src.services.flow_angular import (
    AngularProtocolError, AngularSubmissionUncertain,
    build_image_upsample_rpc, upscaled_image,
)
from src.services.flow_client import FlowClient


class FlowImageUpsampleTests(unittest.TestCase):
    def setUp(self):
        stream = io.BytesIO()
        Image.new("RGB", (32, 32), "white").save(stream, format="JPEG")
        self.encoded = base64.b64encode(stream.getvalue()).decode("ascii")

    def test_current_flow_wire_shape_and_bound_jpeg_response(self):
        rpc, payload = build_image_upsample_rpc(
            "project", "captcha", "media", "UPSAMPLE_IMAGE_RESOLUTION_2K",
        )
        self.assertEqual(rpc, "SPrCad")
        self.assertEqual(payload, ["media", 1,
            [None, 22, None, None, None, "project", None, None, None, None, ["captcha", 1]]])
        self.assertEqual(build_image_upsample_rpc(
            "project", "captcha", "media", "UPSAMPLE_IMAGE_RESOLUTION_4K",
        )[1][1], 2)
        self.assertEqual(upscaled_image(
            [["media", "project"], self.encoded], "project", "media",
        ), self.encoded)
        self.assertEqual(upscaled_image(
            [["new-upscaled-media", "project"], self.encoded], "project", "media",
        ), self.encoded)

    def test_rejects_wrong_media_or_invalid_image(self):
        with self.assertRaises(AngularProtocolError):
            build_image_upsample_rpc("project", "captcha", "media", "8K")
        for response in (
            [["", "project"], self.encoded],
            [["media", "other"], self.encoded],
            [["media", "project"], base64.b64encode(b"not a jpeg" * 30).decode()],
            [["media", "project"], "bad-data"],
        ):
            with self.subTest(response=response[0]):
                with self.assertRaises(AngularSubmissionUncertain):
                    upscaled_image(response, "project", "media")


class FlowClientImageUpsampleTests(unittest.IsolatedAsyncioTestCase):
    async def test_flow_account_uses_same_browser_rpc_and_not_labs(self):
        stream = io.BytesIO()
        Image.new("RGB", (32, 32), "white").save(stream, format="JPEG")
        encoded = base64.b64encode(stream.getvalue()).decode("ascii")
        client = FlowClient(None, db=object())
        client.uses_flow_session = AsyncMock(return_value=True)
        client._flow_submission_account = AsyncMock(return_value=1)
        client._get_recaptcha_token = AsyncMock(return_value=("captcha", "native:1"))
        client._notify_browser_captcha_request_finished = AsyncMock()
        service = unittest.mock.Mock()
        service.fetch_json = AsyncMock(return_value={
            "rpc_payload": [["media", "project"], encoded],
        })
        with patch("src.services.browser_captcha_native_cdp.BrowserCaptchaService.get_instance",
                   new_callable=AsyncMock, return_value=service):
            result = await client.upsample_image(
                at="", project_id="project", media_id="media",
                target_resolution="UPSAMPLE_IMAGE_RESOLUTION_2K", token_id=1,
            )
        self.assertEqual(result, encoded)
        client._get_recaptcha_token.assert_awaited_once_with(
            "project", action="IMAGE_GENERATION", token_id=1,
        )
        self.assertEqual(service.fetch_json.await_args.kwargs["json_data"], {
            "rpc_id": "SPrCad", "payload": ["media", 1,
                [None, 22, None, None, None, "project", None, None, None, None, ["captcha", 1]]],
        })
        client._notify_browser_captcha_request_finished.assert_awaited_once_with("native:1")
