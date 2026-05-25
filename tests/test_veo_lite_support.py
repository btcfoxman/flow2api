import types
import unittest
from unittest.mock import AsyncMock, patch

from src.core.model_resolver import resolve_model_name
from src.services.flow_client import FlowClient
from src.services.generation_handler import (
    MODEL_CONFIG,
    GenerationHandler,
    _validate_reference_video_duration,
    _video_end_frame_index_from_bytes,
    _video_generation_failure_response,
)


class VeoLiteModelResolverTests(unittest.TestCase):
    def test_resolve_t2v_lite_alias_to_portrait_variant(self):
        request = types.SimpleNamespace(
            generationConfig=types.SimpleNamespace(aspectRatio="portrait")
        )

        resolved = resolve_model_name(
            "veo_3_1_t2v_lite",
            request=request,
            model_config=MODEL_CONFIG,
        )

        self.assertEqual(resolved, "veo_3_1_t2v_lite_portrait")

    def test_resolve_quality_4s_upsample_alias_to_portrait_variant(self):
        request = types.SimpleNamespace(
            generationConfig=types.SimpleNamespace(aspectRatio="portrait")
        )

        resolved = resolve_model_name(
            "veo_3_1_t2v_4s_4k",
            request=request,
            model_config=MODEL_CONFIG,
        )

        self.assertEqual(resolved, "veo_3_1_t2v_portrait_4s_4k")

    def test_resolve_video_image_size_to_upsample_variant(self):
        request = types.SimpleNamespace(
            generationConfig=types.SimpleNamespace(aspectRatio="landscape", imageSize="1080p")
        )

        resolved = resolve_model_name(
            "veo_3_1_i2v_s_6s",
            request=request,
            model_config=MODEL_CONFIG,
        )

        self.assertEqual(resolved, "veo_3_1_i2v_s_6s_1080p")

    def test_resolve_abra_r2v_direct_model(self):
        resolved = resolve_model_name(
            "abra_r2v_10s",
            model_config=MODEL_CONFIG,
        )

        self.assertEqual(resolved, "abra_r2v_10s")


class VeoLiteGenerationHandlerTests(unittest.TestCase):
    @staticmethod
    def _mp4_with_duration(duration_seconds: int, timescale: int = 1000) -> bytes:
        duration = duration_seconds * timescale
        mvhd_payload = (
            b"\x00\x00\x00\x00"
            + (0).to_bytes(4, "big")
            + (0).to_bytes(4, "big")
            + timescale.to_bytes(4, "big")
            + duration.to_bytes(4, "big")
            + b"\x00" * 80
        )
        mvhd = (len(mvhd_payload) + 8).to_bytes(4, "big") + b"mvhd" + mvhd_payload
        moov = (len(mvhd) + 8).to_bytes(4, "big") + b"moov" + mvhd
        ftyp = (16).to_bytes(4, "big") + b"ftyp" + b"isom" + b"\x00\x00\x00\x01"
        return ftyp + moov

    def test_video_edit_frame_index_uses_uploaded_video_duration(self):
        self.assertEqual(_video_end_frame_index_from_bytes(self._mp4_with_duration(4)), 96)
        self.assertEqual(_video_end_frame_index_from_bytes(self._mp4_with_duration(10)), 239)
        self.assertEqual(_video_end_frame_index_from_bytes(self._mp4_with_duration(20)), 239)

    def test_reference_video_duration_limit_is_ten_seconds(self):
        self.assertEqual(_validate_reference_video_duration(self._mp4_with_duration(10)), 10.0)
        with self.assertRaisesRegex(ValueError, "参考视频不能超过10秒"):
            _validate_reference_video_duration(self._mp4_with_duration(11))

    def test_video_policy_failure_uses_client_error_status(self):
        message, status_code = _video_generation_failure_response("PUBLIC_ERROR_UNSAFE_GENERATION")

        self.assertEqual(status_code, 400)
        self.assertIn("内容安全策略拒绝", message)
        self.assertIn("PUBLIC_ERROR_UNSAFE_GENERATION", message)

    def test_video_transient_failure_stays_server_error_status(self):
        message, status_code = _video_generation_failure_response("upstream temporary error")

        self.assertEqual(status_code, 502)
        self.assertIn("请重试", message)

    def test_tier_two_does_not_upgrade_lite_model_to_fake_ultra(self):
        handler = GenerationHandler.__new__(GenerationHandler)

        model_key, message = handler._resolve_video_model_key_for_tier(
            {
                "model_key": "veo_3_1_t2v_lite",
                "allow_tier_upgrade": False,
            },
            "PAYGATE_TIER_TWO",
        )

        self.assertEqual(model_key, "veo_3_1_t2v_lite")
        self.assertIsNone(message)

    def test_tier_two_still_upgrades_regular_model(self):
        handler = GenerationHandler.__new__(GenerationHandler)

        model_key, message = handler._resolve_video_model_key_for_tier(
            {
                "model_key": "veo_3_1_t2v_fast",
            },
            "PAYGATE_TIER_TWO",
        )

        self.assertEqual(model_key, "veo_3_1_t2v_fast_ultra")
        self.assertIn("ultra", message)

    def test_quality_model_does_not_upgrade_to_fake_ultra(self):
        handler = GenerationHandler.__new__(GenerationHandler)

        model_key, message = handler._resolve_video_model_key_for_tier(
            {
                "model_key": "veo_3_1_t2v",
            },
            "PAYGATE_TIER_TWO",
        )

        self.assertEqual(model_key, "veo_3_1_t2v")
        self.assertIsNone(message)

    def test_quality_4s_upsample_model_generates_then_upsamples(self):
        cfg = MODEL_CONFIG["veo_3_1_t2v_4s_4k"]

        self.assertEqual(cfg["model_key"], "veo_3_1_t2v_quality_4s")
        self.assertEqual(cfg["video_type"], "t2v")
        self.assertEqual(cfg["upsample"]["model_key"], "veo_3_1_upsampler_4k")
        self.assertEqual(cfg["upsample"]["resolution"], "VIDEO_RESOLUTION_4K")

    def test_quality_6s_i2v_1080p_model_generates_then_upsamples(self):
        cfg = MODEL_CONFIG["veo_3_1_i2v_s_6s_1080p"]

        self.assertEqual(cfg["model_key"], "veo_3_1_i2v_s_quality_6s_fl")
        self.assertEqual(cfg["video_type"], "i2v")
        self.assertEqual(cfg["upsample"]["model_key"], "veo_3_1_upsampler_1080p")
        self.assertEqual(cfg["upsample"]["resolution"], "VIDEO_RESOLUTION_1080P")

    def test_short_duration_models_include_explicit_landscape_aliases(self):
        expected_aliases = {
            "veo_3_1_t2v_landscape_4s": "veo_3_1_t2v_4s",
            "veo_3_1_t2v_landscape_6s": "veo_3_1_t2v_6s",
            "veo_3_1_i2v_s_landscape_4s": "veo_3_1_i2v_s_4s",
            "veo_3_1_i2v_s_landscape_6s": "veo_3_1_i2v_s_6s",
            "veo_3_1_t2v_landscape_4s_4k": "veo_3_1_t2v_4s_4k",
            "veo_3_1_i2v_s_landscape_6s_1080p": "veo_3_1_i2v_s_6s_1080p",
        }

        for alias, target in expected_aliases.items():
            self.assertIn(alias, MODEL_CONFIG)
            self.assertEqual(MODEL_CONFIG[alias], MODEL_CONFIG[target])

    def test_r2v_models_include_explicit_landscape_aliases(self):
        expected_aliases = {
            "veo_3_1_r2v_fast_landscape": "veo_3_1_r2v_fast",
            "veo_3_1_r2v_fast_landscape_ultra": "veo_3_1_r2v_fast_ultra",
            "veo_3_1_r2v_fast_landscape_ultra_relaxed": "veo_3_1_r2v_fast_ultra_relaxed",
            "veo_3_1_r2v_fast_landscape_ultra_4k": "veo_3_1_r2v_fast_ultra_4k",
            "veo_3_1_r2v_fast_landscape_ultra_1080p": "veo_3_1_r2v_fast_ultra_1080p",
        }

        for alias, target in expected_aliases.items():
            self.assertIn(alias, MODEL_CONFIG)
            self.assertEqual(MODEL_CONFIG[alias], MODEL_CONFIG[target])

    def test_abra_r2v_is_direct_public_model_without_replacing_existing_r2v(self):
        for seconds in (4, 6, 8, 10):
            cfg = MODEL_CONFIG[f"abra_r2v_{seconds}s"]
            self.assertEqual(cfg["model_key"], f"abra_r2v_{seconds}s")
            self.assertEqual(cfg["video_type"], "r2v")
            self.assertEqual(cfg["aspect_ratio"], "VIDEO_ASPECT_RATIO_LANDSCAPE")
            self.assertEqual(cfg["min_images"], 1)
            self.assertEqual(cfg["max_images"], 7)
            self.assertTrue(cfg["use_v2_model_config"])
            self.assertTrue(cfg["suppress_scene_id_metadata"])
            self.assertTrue(cfg["allow_aspect_ratio_override"])

        self.assertEqual(
            MODEL_CONFIG["veo_3_1_r2v_fast_portrait"]["model_key"],
            "veo_3_1_r2v_fast_portrait",
        )
        self.assertNotEqual(MODEL_CONFIG["veo_3_1_r2v_fast"]["model_key"], "abra_r2v_10s")

    def test_abra_t2v_is_direct_public_text_model(self):
        for seconds in (4, 6, 8, 10):
            cfg = MODEL_CONFIG[f"abra_t2v_{seconds}s"]
            self.assertEqual(cfg["model_key"], f"abra_t2v_{seconds}s")
            self.assertEqual(cfg["video_type"], "t2v")
            self.assertEqual(cfg["aspect_ratio"], "VIDEO_ASPECT_RATIO_LANDSCAPE")
            self.assertFalse(cfg["supports_images"])
            self.assertTrue(cfg["use_v2_model_config"])
            self.assertTrue(cfg["allow_aspect_ratio_override"])

    def test_abra_edit_is_direct_public_model(self):
        cfg = MODEL_CONFIG["abra_edit"]

        self.assertEqual(cfg["model_key"], "abra_edit")
        self.assertEqual(cfg["video_type"], "v2v")
        self.assertEqual(cfg["aspect_ratio"], "VIDEO_ASPECT_RATIO_LANDSCAPE")
        self.assertEqual(cfg["min_images"], 1)
        self.assertEqual(cfg["max_images"], 5)
        self.assertTrue(cfg["supports_images"])
        self.assertTrue(cfg["requires_video_input"])
        self.assertTrue(cfg["allow_aspect_ratio_override"])

    def test_direct_upsampler_keys_are_not_public_models(self):
        self.assertNotIn("veo_3_1_upsampler_4k", MODEL_CONFIG)
        self.assertNotIn("veo_3_1_upsampler_1080p", MODEL_CONFIG)


class VeoLiteFlowClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = FlowClient(proxy_manager=None)
        self.client._acquire_video_launch_gate = AsyncMock(return_value=(True, None, None))
        self.client._release_video_launch_gate = AsyncMock()
        self.client._get_recaptcha_token = AsyncMock(return_value=("recaptcha-token", "browser-1"))
        self.client._notify_browser_captcha_request_finished = AsyncMock()

    async def test_generate_video_text_uses_v2_payload_for_lite(self):
        captured = {}

        async def fake_make_request(method, url, json_data, use_at, at_token, **kwargs):
            captured["url"] = url
            captured["json_data"] = json_data
            return {"operations": [{"operation": {"name": "task-1"}}]}

        self.client._make_request = AsyncMock(side_effect=fake_make_request)

        await self.client.generate_video_text(
            at="at-token",
            project_id="project-1",
            prompt="猫猫",
            model_key="veo_3_1_t2v_lite",
            aspect_ratio="VIDEO_ASPECT_RATIO_LANDSCAPE",
            use_v2_model_config=True,
        )

        json_data = captured["json_data"]
        request_data = json_data["requests"][0]
        self.assertTrue(json_data["useV2ModelConfig"])
        self.assertIn("batchId", json_data["mediaGenerationContext"])
        self.assertEqual(
            request_data["textInput"]["structuredPrompt"]["parts"][0]["text"],
            "猫猫",
        )
        self.assertNotIn("prompt", request_data["textInput"])
        self.assertEqual(request_data["videoModelKey"], "veo_3_1_t2v_lite")
        self.assertEqual(
            json_data["mediaGenerationContext"]["audioFailurePreference"],
            "BLOCK_SILENCED_VIDEOS",
        )

    async def test_generate_video_text_uses_abra_t2v_model_key(self):
        captured = {}

        async def fake_make_request(method, url, json_data, use_at, at_token, **kwargs):
            captured["url"] = url
            captured["json_data"] = json_data
            return {"operations": [{"operation": {"name": "task-abra-t2v"}}]}

        self.client._make_request = AsyncMock(side_effect=fake_make_request)

        await self.client.generate_video_text(
            at="at-token",
            project_id="project-1",
            prompt="text only",
            model_key="abra_t2v_10s",
            aspect_ratio="VIDEO_ASPECT_RATIO_LANDSCAPE",
            use_v2_model_config=True,
        )

        self.assertTrue(captured["url"].endswith("/video:batchAsyncGenerateVideoText"))
        request_data = captured["json_data"]["requests"][0]
        self.assertEqual(request_data["videoModelKey"], "abra_t2v_10s")
        self.assertEqual(
            request_data["textInput"]["structuredPrompt"]["parts"][0]["text"],
            "text only",
        )
        self.assertNotIn("duration", request_data)

    async def test_generate_video_text_normalizes_media_only_create_response(self):
        captured = {}

        async def fake_make_request(method, url, json_data, use_at, at_token, **kwargs):
            captured["json_data"] = json_data
            return {
                "remainingCredits": 30,
                "workflows": [
                    {
                        "name": "workflow-1",
                        "metadata": {"primaryMediaId": "media-1"},
                        "projectId": "project-1",
                    }
                ],
                "media": [
                    {
                        "name": "media-1",
                        "projectId": "project-1",
                        "mediaMetadata": {
                            "mediaStatus": {
                                "mediaGenerationStatus": "MEDIA_GENERATION_STATUS_PENDING"
                            }
                        },
                    }
                ],
            }

        self.client._make_request = AsyncMock(side_effect=fake_make_request)

        result = await self.client.generate_video_text(
            at="at-token",
            project_id="project-1",
            prompt="猫猫",
            model_key="veo_3_1_t2v_lite",
            aspect_ratio="VIDEO_ASPECT_RATIO_LANDSCAPE",
            use_v2_model_config=True,
        )

        self.assertEqual(
            captured["json_data"]["mediaGenerationContext"]["audioFailurePreference"],
            "BLOCK_SILENCED_VIDEOS",
        )
        self.assertEqual(result["operations"][0]["operation"]["name"], "media-1")
        self.assertEqual(result["operations"][0]["projectId"], "project-1")
        self.assertEqual(
            result["operations"][0]["status"],
            "MEDIA_GENERATION_STATUS_PENDING",
        )

    async def test_check_video_status_uses_media_payload_and_normalizes_response(self):
        captured = {}

        async def fake_make_request(method, url, json_data, use_at, at_token, **kwargs):
            captured["json_data"] = json_data
            return {
                "media": [
                    {
                        "name": "media-1",
                        "projectId": "project-1",
                        "mediaMetadata": {
                            "mediaStatus": {
                                "mediaGenerationStatus": "MEDIA_GENERATION_STATUS_SUCCESSFUL"
                            }
                        },
                        "video": {
                            "fifeUrl": "https://flow-content.google/video/11111111-1111-1111-1111-111111111111?token=abc",
                            "generatedVideo": {
                                "aspectRatio": "VIDEO_ASPECT_RATIO_LANDSCAPE"
                            },
                        },
                    }
                ]
            }

        self.client._make_request = AsyncMock(side_effect=fake_make_request)

        result = await self.client.check_video_status(
            at="at-token",
            operations=[
                {
                    "operation": {"name": "media-1"},
                    "projectId": "project-1",
                }
            ],
        )

        self.assertEqual(
            captured["json_data"],
            {"media": [{"name": "media-1", "projectId": "project-1"}]},
        )
        operation = result["operations"][0]
        self.assertEqual(operation["operation"]["name"], "media-1")
        self.assertEqual(operation["status"], "MEDIA_GENERATION_STATUS_SUCCESSFUL")
        self.assertEqual(
            operation["operation"]["metadata"]["video"]["fifeUrl"],
            "https://flow-content.google/video/11111111-1111-1111-1111-111111111111?token=abc",
        )

    async def test_generate_video_reference_images_uses_abra_capture_payload(self):
        captured = {}

        async def fake_make_request(method, url, json_data, use_at, at_token, **kwargs):
            captured["url"] = url
            captured["json_data"] = json_data
            captured["headers"] = kwargs.get("headers")
            captured["apply_default_client_headers"] = kwargs.get("apply_default_client_headers")
            return {
                "workflows": [
                    {
                        "name": "workflow-1",
                        "metadata": {"primaryMediaId": "media-1"},
                        "projectId": "project-1",
                    }
                ],
                "media": [
                    {
                        "name": "media-1",
                        "projectId": "project-1",
                        "workflowId": "workflow-1",
                        "mediaMetadata": {
                            "mediaStatus": {
                                "mediaGenerationStatus": "MEDIA_GENERATION_STATUS_SCHEDULED"
                            }
                        },
                    }
                ],
            }

        self.client._make_request = AsyncMock(side_effect=fake_make_request)

        result = await self.client.generate_video_reference_images(
            at="at-token",
            project_id="project-1",
            prompt="show product",
            model_key="abra_r2v_8s",
            aspect_ratio="VIDEO_ASPECT_RATIO_LANDSCAPE",
            reference_images=[
                {"mediaId": "image-1", "imageUsageType": "IMAGE_USAGE_TYPE_ASSET"},
                {"mediaId": "image-2", "imageUsageType": "IMAGE_USAGE_TYPE_ASSET"},
            ],
            suppress_scene_id_metadata=True,
        )

        self.assertTrue(captured["url"].endswith("/video:batchAsyncGenerateVideoReferenceImages"))
        self.assertEqual(captured["headers"]["Content-Type"], "text/plain;charset=UTF-8")
        self.assertEqual(captured["headers"]["Referer"], "https://labs.google/")
        self.assertFalse(captured["apply_default_client_headers"])
        json_data = captured["json_data"]
        request_data = json_data["requests"][0]
        self.assertTrue(json_data["useV2ModelConfig"])
        self.assertEqual(
            json_data["mediaGenerationContext"]["audioFailurePreference"],
            "BLOCK_SILENCED_VIDEOS",
        )
        self.assertEqual(request_data["videoModelKey"], "abra_r2v_8s")
        self.assertEqual(request_data["aspectRatio"], "VIDEO_ASPECT_RATIO_LANDSCAPE")
        self.assertEqual(request_data["metadata"], {})
        self.assertEqual(
            request_data["referenceImages"][0]["imageUsageType"],
            "IMAGE_USAGE_TYPE_ASSET",
        )
        self.assertEqual(result["operations"][0]["operation"]["name"], "media-1")
        self.assertEqual(result["operations"][0]["workflowId"], "workflow-1")

    async def test_generate_video_edit_video_uses_abra_capture_payload(self):
        captured = {}

        async def fake_make_request(method, url, json_data, use_at, at_token, **kwargs):
            captured["url"] = url
            captured["json_data"] = json_data
            captured["headers"] = kwargs.get("headers")
            return {
                "media": [
                    {
                        "name": "media-edit-1",
                        "projectId": "project-1",
                        "mediaMetadata": {
                            "mediaStatus": {
                                "mediaGenerationStatus": "MEDIA_GENERATION_STATUS_PENDING"
                            }
                        },
                    }
                ]
            }

        self.client._make_request = AsyncMock(side_effect=fake_make_request)

        result = await self.client.generate_video_edit_video(
            at="at-token",
            project_id="project-1",
            prompt="edit scene",
            model_key="abra_edit",
            aspect_ratio="VIDEO_ASPECT_RATIO_LANDSCAPE",
            video_media_id="video-media-1",
            reference_images=[
                {"mediaId": "image-1", "imageUsageType": "IMAGE_USAGE_TYPE_ASSET"},
            ],
        )

        self.assertTrue(captured["url"].endswith("/video:batchAsyncGenerateVideoEditVideo"))
        self.assertEqual(captured["headers"]["Content-Type"], "text/plain;charset=UTF-8")
        json_data = captured["json_data"]
        request_data = json_data["requests"][0]
        self.assertIn("mediaGenerationContext", json_data)
        self.assertNotIn("useV2ModelConfig", json_data)
        self.assertEqual(request_data["videoModelKey"], "abra_edit")
        self.assertEqual(request_data["metadata"], {})
        self.assertEqual(
            request_data["videoInput"],
            {
                "mediaId": "video-media-1",
                "startFrameIndex": 0,
                "endFrameIndex": 239,
            },
        )
        self.assertEqual(request_data["referenceImages"][0]["mediaId"], "image-1")
        self.assertNotIn("duration", request_data)
        self.assertEqual(result["operations"][0]["operation"]["name"], "media-edit-1")

    async def test_generate_video_edit_video_uses_dynamic_video_end_frame(self):
        captured = {}

        async def fake_make_request(method, url, json_data, use_at, at_token, **kwargs):
            captured["json_data"] = json_data
            return {"media": [{"name": "media-edit-1"}]}

        self.client._make_request = AsyncMock(side_effect=fake_make_request)

        await self.client.generate_video_edit_video(
            at="at-token",
            project_id="project-1",
            prompt="edit scene",
            model_key="abra_edit",
            aspect_ratio="VIDEO_ASPECT_RATIO_LANDSCAPE",
            video_media_id="video-media-1",
            reference_images=[{"mediaId": "image-1", "imageUsageType": "IMAGE_USAGE_TYPE_ASSET"}],
            video_end_frame_index=96,
        )

        request_data = captured["json_data"]["requests"][0]
        self.assertEqual(request_data["videoInput"]["startFrameIndex"], 0)
        self.assertEqual(request_data["videoInput"]["endFrameIndex"], 96)

    async def test_successful_media_status_without_fife_url_keeps_media_and_workflow_metadata(self):
        result = self.client._normalize_video_generation_response(
            {
                "workflows": [
                    {
                        "name": "workflow-1",
                        "metadata": {"primaryMediaId": "media-1"},
                        "projectId": "project-1",
                    }
                ],
                "media": [
                    {
                        "name": "media-1",
                        "projectId": "project-1",
                        "workflowId": "workflow-1",
                        "mediaMetadata": {
                            "mediaStatus": {
                                "mediaGenerationStatus": "MEDIA_GENERATION_STATUS_SUCCESSFUL"
                            }
                        },
                        "video": {
                            "generatedVideo": {
                                "aspectRatio": "VIDEO_ASPECT_RATIO_PORTRAIT"
                            },
                            "operation": {"name": "media-1"},
                        },
                    }
                ],
            }
        )

        operation = result["operations"][0]
        self.assertEqual(operation["operation"]["name"], "media-1")
        self.assertEqual(operation["workflowId"], "workflow-1")
        self.assertEqual(operation["status"], "MEDIA_GENERATION_STATUS_SUCCESSFUL")
        self.assertEqual(
            operation["operation"]["metadata"]["video"]["mediaGenerationId"],
            "media-1",
        )
        self.assertEqual(
            operation["operation"]["metadata"]["video"]["workflowId"],
            "workflow-1",
        )

    async def test_get_media_url_redirect_uses_observed_redirect_headers(self):
        captured = {}

        class FakeResponse:
            status_code = 307
            headers = {
                "location": "https://flow-content.google/video/media-1?token=abc"
            }
            text = ""

        class FakeSession:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def get(self, url, headers, proxy, timeout, allow_redirects, impersonate):
                captured["url"] = url
                captured["headers"] = headers
                captured["allow_redirects"] = allow_redirects
                return FakeResponse()

        with patch("src.services.flow_client.AsyncSession", FakeSession):
            result = await self.client.get_media_url_redirect(
                st="session-token",
                media_id="media-1",
                project_id="project-1",
            )

        self.assertEqual(result, "https://flow-content.google/video/media-1?token=abc")
        self.assertTrue(captured["url"].endswith("/trpc/media.getMediaUrlRedirect?name=media-1"))
        self.assertFalse(captured["allow_redirects"])
        self.assertEqual(
            captured["headers"]["Referer"],
            "https://labs.google/fx/zh/tools/flow/project/project-1",
        )
        self.assertEqual(
            captured["headers"]["Cookie"],
            "__Secure-next-auth.session-token=session-token",
        )
        self.assertEqual(captured["headers"]["Accept-Encoding"], "identity;q=1, *;q=0")
        self.assertEqual(captured["headers"]["Range"], "bytes=0-")
        self.assertNotIn("Accept", captured["headers"])

    async def test_upload_video_uses_observed_start_and_chunk_protocol(self):
        captured = {"post": None, "puts": []}

        class FakeResponse:
            def __init__(self, status_code, payload):
                self.status_code = status_code
                self._payload = payload
                self.headers = {}
                self.text = "{}"

            def json(self):
                return self._payload

        class FakeSession:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def post(self, url, headers, proxy, timeout, impersonate):
                captured["post"] = {
                    "url": url,
                    "headers": headers,
                    "proxy": proxy,
                    "timeout": timeout,
                    "impersonate": impersonate,
                }
                return FakeResponse(200, {"sessionUrl": "https://upload-session", "status": "active"})

            async def put(self, url, headers, data, proxy, timeout, impersonate):
                captured["puts"].append({
                    "url": url,
                    "headers": headers,
                    "data_len": len(data),
                    "proxy": proxy,
                    "timeout": timeout,
                    "impersonate": impersonate,
                })
                if "finalize" in headers["x-upload-command"]:
                    return FakeResponse(
                        200,
                        {
                            "status": "final",
                            "mediaServerId": "video-media-1",
                            "videoWidth": 1280,
                            "videoHeight": 720,
                        },
                    )
                return FakeResponse(200, {"status": "active"})

        video_bytes = b"x" * (2 * 1024 * 1024 + 5)
        with patch("src.services.flow_client.AsyncSession", FakeSession):
            media_id = await self.client.upload_video(
                st="session-token",
                project_id="project-1",
                video_bytes=video_bytes,
                mime_type="video/mp4",
                file_name="sample.mp4",
            )

        self.assertEqual(media_id, "video-media-1")
        self.assertTrue(captured["post"]["url"].endswith("/upload-video?action=start"))
        self.assertEqual(captured["post"]["headers"]["x-upload-content-length"], str(len(video_bytes)))
        self.assertEqual(captured["post"]["headers"]["x-upload-content-type"], "video/mp4")
        self.assertEqual(captured["post"]["headers"]["x-upload-file-name"], "sample.mp4")
        self.assertEqual(captured["post"]["headers"]["x-upload-project-id"], "project-1")
        self.assertEqual(captured["post"]["headers"]["Cookie"], "__Secure-next-auth.session-token=session-token")
        self.assertEqual(len(captured["puts"]), 2)
        self.assertEqual(captured["puts"][0]["headers"]["x-upload-command"], "upload")
        self.assertEqual(captured["puts"][0]["headers"]["x-upload-offset"], "0")
        self.assertEqual(captured["puts"][0]["headers"]["Content-Type"], "application/octet-stream")
        self.assertEqual(captured["puts"][0]["data_len"], 2 * 1024 * 1024)
        self.assertEqual(captured["puts"][1]["headers"]["x-upload-command"], "upload, finalize")
        self.assertEqual(captured["puts"][1]["headers"]["x-upload-offset"], str(2 * 1024 * 1024))
        self.assertEqual(captured["puts"][1]["headers"]["x-upload-session-url"], "https://upload-session")

    async def test_make_request_can_skip_unobserved_synthetic_browser_headers(self):
        captured = {}

        class FakeResponse:
            status_code = 200
            headers = {}
            text = "{}"

            def json(self):
                return {}

        class FakeSession:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def post(self, url, headers, json, proxy, timeout, impersonate):
                captured["headers"] = headers
                return FakeResponse()

        self.client._set_request_fingerprint({
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
            "accept_language": "en-US",
            "sec_ch_ua": '"Google Chrome";v="135", "Not-A.Brand";v="8", "Chromium";v="135"',
            "sec_ch_ua_mobile": "?0",
            "sec_ch_ua_platform": '"Windows"',
        })

        with patch("src.services.flow_client.AsyncSession", FakeSession):
            await self.client._make_request(
                method="POST",
                url="https://aisandbox-pa.googleapis.com/v1/video:batchAsyncGenerateVideoReferenceImages",
                headers={
                    "Content-Type": "text/plain;charset=UTF-8",
                    "Referer": "https://labs.google/",
                },
                json_data={},
                use_at=True,
                at_token="at-token",
                apply_default_client_headers=False,
            )

        self.assertEqual(captured["headers"]["Referer"], "https://labs.google/")
        self.assertEqual(captured["headers"]["sec-ch-ua-platform"], '"Windows"')
        self.assertNotIn("x-browser-channel", captured["headers"])
        self.assertNotIn("x-browser-year", captured["headers"])
        self.assertNotIn("sec-fetch-site", captured["headers"])

    async def test_create_project_uses_labs_same_origin_headers(self):
        captured = {}

        class FakeResponse:
            status_code = 200
            headers = {}
            text = '{"result":{"data":{"json":{"result":{"projectId":"project-1"}}}}}'

            def json(self):
                return {
                    "result": {
                        "data": {
                            "json": {
                                "result": {
                                    "projectId": "project-1",
                                }
                            }
                        }
                    }
                }

        class FakeSession:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def post(self, url, headers, json, proxy, timeout, impersonate):
                captured["url"] = url
                captured["headers"] = headers
                captured["json"] = json
                return FakeResponse()

        with patch("src.services.flow_client.AsyncSession", FakeSession):
            project_id = await self.client.create_project("session-token", "Project 1")

        self.assertEqual(project_id, "project-1")
        self.assertTrue(captured["url"].endswith("/trpc/project.createProject"))
        self.assertEqual(captured["headers"]["Cookie"], "__Secure-next-auth.session-token=session-token")
        self.assertEqual(captured["headers"]["Origin"], "https://labs.google")
        self.assertEqual(captured["headers"]["Referer"], "https://labs.google/fx/tools/flow")
        self.assertEqual(captured["headers"]["sec-fetch-site"], "same-origin")

    async def test_generate_video_start_end_uses_v2_payload_for_interpolation_lite(self):
        captured = {}

        async def fake_make_request(method, url, json_data, use_at, at_token, **kwargs):
            captured["url"] = url
            captured["json_data"] = json_data
            return {"operations": [{"operation": {"name": "task-2"}}]}

        self.client._make_request = AsyncMock(side_effect=fake_make_request)

        await self.client.generate_video_start_end(
            at="at-token",
            project_id="project-1",
            prompt="变身猫猫",
            model_key="veo_3_1_interpolation_lite",
            aspect_ratio="VIDEO_ASPECT_RATIO_PORTRAIT",
            start_media_id="start-media",
            end_media_id="end-media",
            use_v2_model_config=True,
        )

        json_data = captured["json_data"]
        request_data = json_data["requests"][0]
        self.assertTrue(json_data["useV2ModelConfig"])
        self.assertIn("batchId", json_data["mediaGenerationContext"])
        self.assertEqual(request_data["videoModelKey"], "veo_3_1_interpolation_lite")
        self.assertEqual(request_data["startImage"]["mediaId"], "start-media")
        self.assertEqual(request_data["endImage"]["mediaId"], "end-media")
        self.assertEqual(
            request_data["textInput"]["structuredPrompt"]["parts"][0]["text"],
            "变身猫猫",
        )


class CookieCompatibilityTests(unittest.TestCase):
    def test_flow_client_builds_cookie_header_from_full_cookie_header(self):
        client = FlowClient(proxy_manager=None)

        cookie_header = client._build_labs_cookie_header(
            "__Host-next-auth.csrf-token=csrf; "
            "__Secure-next-auth.callback-url=https%3A%2F%2Flabs.google%2F; "
            "email=user@example.com; "
            "__Secure-next-auth.session-token=session; "
            "EMAIL=user@example.com"
        )

        self.assertIn("__Secure-next-auth.session-token=session", cookie_header)
        self.assertIn("__Host-next-auth.csrf-token=csrf", cookie_header)
        self.assertIn("__Secure-next-auth.callback-url=https%3A%2F%2Flabs.google%2F", cookie_header)

    def test_flow_client_builds_cookie_header_from_cookies_json_payload(self):
        client = FlowClient(proxy_manager=None)

        cookie_header = client._build_labs_cookie_header(
            [
                {"name": "_ga", "value": "GA1.1.1.1", "domain": ".labs.google", "path": "/"},
                {
                    "name": "__Secure-next-auth.session-token",
                    "value": "session",
                    "domain": "labs.google",
                    "path": "/",
                    "secure": True,
                    "httpOnly": True,
                    "sameSite": "Lax",
                },
            ]
        )

        self.assertIn("_ga=GA1.1.1.1", cookie_header)
        self.assertIn("__Secure-next-auth.session-token=session", cookie_header)


if __name__ == "__main__":
    unittest.main()
