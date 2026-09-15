"""Text-video capture parity and native routing; no live submissions or charges."""
import asyncio
import copy
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.core.generation_errors import NativeSessionError
from src.services.browser_captcha_native_cdp import BrowserCaptchaService, NativeCdpAccountBrowser
from src.services.flow_angular import (
    AngularProtocolError, AngularRpcRejected, AngularSubmissionUncertain,
    FLOW_RPC_URL, GENERATION_RPC_IDS, MUTATING_RPC_IDS, RPC_IDS,
    build_video_rpc, parse_rpc_response, resolve_video_model, rpc_fetch_expression,
    use_angular_video, video_operations,
)
from src.services.flow_client import FlowClient
from src.services.generation_handler import MODEL_CONFIG
from src.services.model_capabilities import supports_flow_model


FIXTURE = json.loads((Path(__file__).parent / "fixtures/flow_t2v_20260915.json").read_text(encoding="utf-8"))
MODELS = [f"abra_t2v_{seconds}s{suffix}"
          for seconds in (4, 6, 8, 10) for suffix in ("", "_720p", "_360p")]


def request(model="abra_t2v_4s_360p", aspect="VIDEO_ASPECT_RATIO_PORTRAIT"):
    return {
        "clientContext": {"projectId": "project-test", "recaptchaContext": {"token": "test-captcha"}},
        "requests": [{"videoModelKey": model, "aspectRatio": aspect,
                      "textInput": {"structuredPrompt": {"parts": [{"text": "A dog digging."}]}},
                      "outputSpec": {"resolution": MODEL_CONFIG[model]["output_resolution"]}}],
        "mediaGenerationContext": {"batchId": "batch-test"},
    }


class TextVideoWireTests(unittest.TestCase):
    def test_request_matches_manual_capture_exactly(self):
        rest = request()
        before = copy.deepcopy(rest)
        with patch("src.services.flow_angular.uuid.uuid4", side_effect=["metadata-id-1", "metadata-id-2"]):
            rpc, payload = build_video_rpc(rest)
        self.assertEqual(rpc, FIXTURE["rpc_id"])
        self.assertEqual(payload, FIXTURE["request"])
        self.assertEqual(rest, before)

    def test_all_catalog_durations_resolutions_and_aspects_use_text_shape(self):
        for model in MODELS:
            for aspect, enum in [("VIDEO_ASPECT_RATIO_LANDSCAPE", 2), ("VIDEO_ASPECT_RATIO_PORTRAIT", 1)]:
                with self.subTest(model=model, aspect=aspect):
                    rpc, payload = build_video_rpc(request(model, aspect))
                    item = payload[0][0]
                    self.assertEqual(rpc, "YhhmEf")
                    self.assertEqual(item[0], [None, None, [[["A dog digging."]]]])
                    self.assertEqual(item[1], MODEL_CONFIG[model]["model_key"])
                    self.assertEqual(item[2], enum)
                    self.assertIsNone(item[3])
                    self.assertEqual(len(item[4]), 6)
                    if model.endswith("_360p"):
                        self.assertEqual(item[5:], [None, None, [4]])
                    else:
                        self.assertEqual(len(item), 5)
                    self.assertTrue(supports_flow_model(MODEL_CONFIG[model]))
                    self.assertTrue(use_angular_video(model, families=["abra_t2v"]))
                    self.assertFalse(use_angular_video(model, families=["abra_r2v", "abra_edit"]))

    def test_omitted_resolution_and_aspect_use_public_defaults(self):
        for model in MODELS:
            rest = request(model)
            del rest["requests"][0]["outputSpec"]
            del rest["requests"][0]["aspectRatio"]
            item = build_video_rpc(rest)[1][0][0]
            self.assertEqual(item[2], 2)
            self.assertEqual(len(item), 8 if model.endswith("_360p") else 5)

    def test_exact_opt_in_stays_resolution_specific(self):
        for seconds in (4, 6, 8, 10):
            model = f"abra_t2v_{seconds}s"
            self.assertTrue(use_angular_video(model, models=[model + "_720p"]))
            self.assertTrue(use_angular_video(model + "_720p", models=[model]))
            self.assertTrue(use_angular_video(model + "_360p", models=[model + "_360p"]))
            self.assertFalse(use_angular_video(model, models=[model + "_360p"]))
            self.assertFalse(use_angular_video(model + "_360p", models=[model + "_720p"]))
        for model in ("abra_t2v_5s", "abra_t2v_4s_1080p", "abra_t2v_future", "veo_3_1_t2v_fast"):
            self.assertIsNone(resolve_video_model(model))

    def test_invalid_input_is_rejected_before_submission(self):
        for key, value in [
            ("referenceImages", [{"mediaId": "image"}]), ("videoInput", {"mediaId": "video"}),
            ("startImage", {"mediaId": "image"}), ("endImage", {"mediaId": "image"}),
            ("imageInputs", [{"name": "image"}]),
            ("outputSpec", {"resolution": "VIDEO_RESOLUTION_720P"}),
            ("outputSpec", {"resolution": "VIDEO_RESOLUTION_1080P"}),
            ("outputSpec", {"unverified": True}), ("aspectRatio", "VIDEO_ASPECT_RATIO_SQUARE"),
            ("textInput", {"structuredPrompt": {"parts": [{"text": 123}]}}),
        ]:
            with self.subTest(key=key, value=value):
                rest = request()
                rest["requests"][0][key] = value
                with self.assertRaises(AngularProtocolError):
                    build_video_rpc(rest)
        rest = request()
        rest["requests"] *= 2
        with self.assertRaises(AngularProtocolError):
            build_video_rpc(rest)

    def test_capture_submit_poll_and_signed_result_are_compatible(self):
        for key, status in [("submit", "PENDING"), ("active", "ACTIVE"),
                            ("complete", "SUCCESSFUL"), ("detail", "SUCCESSFUL")]:
            rpc = "YhhmEf" if key == "submit" else "as29s" if key == "detail" else "jwpduf"
            raw = ")]}'\n" + json.dumps([["wrb.fr", rpc, json.dumps(FIXTURE[key])]])
            parsed = parse_rpc_response(raw, rpc)
            result = video_operations(parsed, token_id=9, project_id="project-test", expected_ids=["media-test"])
            operation = result["operations"][0]
            self.assertEqual(operation["status"], "MEDIA_GENERATION_STATUS_" + status)
            self.assertEqual(operation["tokenId"], 9)
            if key == "complete":
                self.assertNotIn("fifeUrl", operation["operation"]["metadata"]["video"])
            if key == "detail":
                self.assertIn("/video/media-test?", operation["operation"]["metadata"]["video"]["fifeUrl"])

    def test_text_rpc_is_allowlisted_as_chargeable_mutation(self):
        for registry in (RPC_IDS, MUTATING_RPC_IDS, GENERATION_RPC_IDS):
            self.assertIn("YhhmEf", registry)
        self.assertIn('"YhhmEf"', rpc_fetch_expression("YhhmEf", FIXTURE["request"], 30))


class TextVideoRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = SimpleNamespace(get_token=AsyncMock(return_value=SimpleNamespace(auth_mode="flow")))
        self.client = FlowClient(None, self.db)
        self.client._set_request_fingerprint({"native_token_id": 9})
        self.addCleanup(self.client.clear_request_fingerprint)
        self.client._make_request = AsyncMock(side_effect=AssertionError("No legacy fallback"))
        self.client._get_recaptcha_token = AsyncMock(return_value=("test-captcha", "native:9"))
        self.client._acquire_video_launch_gate = AsyncMock(return_value=(True, 0, 0))
        self.client._release_video_launch_gate = AsyncMock()
        self.client._notify_browser_captcha_request_finished = AsyncMock()
        self.service = SimpleNamespace(fetch_json=AsyncMock(return_value={"rpc_payload": FIXTURE["submit"]}))
        for patcher in [
            patch("src.services.flow_client.config", SimpleNamespace(captcha_method="native_cdp",
                  flow_angular_video_models=[], flow_angular_video_families=[])),
            patch("src.services.browser_captcha_native_cdp.BrowserCaptchaService.get_instance",
                  AsyncMock(return_value=self.service)),
            patch.object(self.client, "_captcha_aware_max_retries", return_value=3),
        ]:
            patcher.start()
            self.addCleanup(patcher.stop)

    async def generate(self, model="abra_t2v_4s_360p"):
        return await self.client.generate_video_text(
            at=None, project_id="project-test", prompt="A dog digging.", model_key=model,
            aspect_ratio="VIDEO_ASPECT_RATIO_PORTRAIT", use_v2_model_config=True,
            output_resolution=MODEL_CONFIG[model]["output_resolution"], token_id=9)

    async def test_flow_account_uses_new_rpc_even_without_legacy_opt_in(self):
        for model in MODELS:
            with self.subTest(model=model):
                result = await self.generate(model)
                sent = self.service.fetch_json.await_args.kwargs
                self.assertEqual(sent["url"], FLOW_RPC_URL)
                self.assertEqual(sent["json_data"]["rpc_id"], "YhhmEf")
                self.assertEqual(sent["json_data"]["payload"][0][0][1], MODEL_CONFIG[model]["model_key"])
                self.assertEqual(sent["token_id"], 9)
                self.assertTrue(sent["consume_video_reservation"])
                self.assertNotIn("headers", sent)
                self.assertEqual(result["operations"][0]["transport"], "angular")
                self.client._get_recaptcha_token.assert_awaited_with(
                    "project-test", action="VIDEO_GENERATION", token_id=9, model_key=model)
        self.client._make_request.assert_not_awaited()

    async def test_unknown_response_is_not_retried_or_fallen_back(self):
        self.service.fetch_json.return_value = {"rpc_payload": []}
        with self.assertRaises(AngularSubmissionUncertain):
            await self.generate()
        self.service.fetch_json.assert_awaited_once()
        self.client._get_recaptcha_token.assert_awaited_once()
        self.client._notify_browser_captcha_request_finished.assert_awaited_once_with("native:9")
        self.client._make_request.assert_not_awaited()

    async def test_wrong_bound_account_is_rejected_without_submit(self):
        self.client._set_request_fingerprint({"native_token_id": 10})
        with self.assertRaises(NativeSessionError):
            await self.generate()
        self.service.fetch_json.assert_not_awaited()

    async def test_native_cdp_timeout_is_treated_as_unknown_submission(self):
        browser = NativeCdpAccountBrowser(9, None)
        browser._prepare_profile = AsyncMock()
        browser._get_or_create_project_session = AsyncMock(return_value=("target", "session"))
        browser._evaluate = AsyncMock(side_effect=asyncio.TimeoutError())
        with self.assertRaises(AngularSubmissionUncertain):
            await browser.fetch_json(project_id="project-test", url=FLOW_RPC_URL,
                                     json_data={"rpc_id": "YhhmEf", "payload": FIXTURE["request"]})
        browser._evaluate.assert_awaited_once()
        self.assertEqual(browser.busy_count, 0)

    async def test_text_rpc_traffic_rejection_tracks_its_bound_proxy(self):
        service = BrowserCaptchaService(None)
        worker = NativeCdpAccountBrowser(9, None)
        worker.proxy_binding = SimpleNamespace(url="socks5://127.0.0.1:20001")
        worker.fetch_json = AsyncMock(side_effect=AngularRpcRejected({
            "grpc_code": 8, "public_error": "PUBLIC_ERROR_UNUSUAL_ACTIVITY_TOO_MUCH_TRAFFIC"}))
        service._workers[9] = worker
        service._ensure_capacity = AsyncMock()
        service._record_proxy_risk = AsyncMock()
        worker.stop = AsyncMock()
        try:
            with self.assertRaises(AngularRpcRejected):
                await service.fetch_json(token_id=9, project_id="project-test", url=FLOW_RPC_URL,
                                         json_data={"rpc_id": "YhhmEf", "payload": []})
            service._record_proxy_risk.assert_awaited_once()
            self.assertEqual(service._record_proxy_risk.await_args.kwargs["token_proxy_url"], worker.proxy_binding.url)
        finally:
            await service.close()
