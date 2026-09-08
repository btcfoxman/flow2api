"""Resolution/family parity for the Angular adapter; all requests are mocked."""
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.core.config import Config
from src.services.flow_angular import (
    AngularProtocolError,
    AngularSubmissionUncertain,
    build_video_rpc,
    resolve_video_model,
    use_angular_video,
)
from src.services.flow_client import FlowClient
from src.services.generation_handler import MODEL_CONFIG


ABRA_MODELS = [
    base + suffix
    for base in [*(f"abra_r2v_{seconds}s" for seconds in (4, 6, 8, 10)), "abra_edit"]
    for suffix in ("", "_720p", "_360p")
]


def video_request(model, aspect="VIDEO_ASPECT_RATIO_LANDSCAPE"):
    catalog = MODEL_CONFIG[model]
    request = {
        "videoModelKey": model,
        "aspectRatio": aspect,
        "outputSpec": {"resolution": catalog["output_resolution"]},
        "textInput": {"structuredPrompt": {"parts": [{"text": "prompt"}]}},
        "referenceImages": [{"mediaId": "image-test"}],
    }
    if model.startswith("abra_edit"):
        request["videoInput"] = {"mediaId": "video-test", "startFrameIndex": 2, "endFrameIndex": 96}
    return {
        "clientContext": {"projectId": "project-test", "recaptchaContext": {"token": "captcha"}},
        "requests": [request],
        "mediaGenerationContext": {"batchId": "batch-test"},
    }


class AngularModelTests(unittest.TestCase):
    def test_resolution_duration_and_aspect_matrix_matches_public_catalog(self):
        for model in ABRA_MODELS:
            for aspect, aspect_code in [("VIDEO_ASPECT_RATIO_LANDSCAPE", 2), ("VIDEO_ASPECT_RATIO_PORTRAIT", 1)]:
                with self.subTest(model=model, aspect=aspect):
                    rest = video_request(model, aspect)
                    original = copy.deepcopy(rest)
                    rpc, payload = build_video_rpc(rest)
                    item = payload[0][0]
                    spec = resolve_video_model(model)
                    edit = spec.family == "abra_edit"
                    self.assertEqual(item[2], MODEL_CONFIG[model]["model_key"])
                    self.assertEqual(item[3], aspect_code)
                    self.assertEqual(rpc, "jIps6" if edit else "MZZa6b")
                    self.assertEqual(item[8 if edit else 1], [[None, "image-test"]])
                    self.assertEqual(payload[1][5], "project-test")
                    self.assertEqual(payload[1][10], ["captcha", 1])
                    self.assertEqual(payload[2], ["batch-test", 2])
                    if model.endswith("_360p"):
                        self.assertEqual(len(item), 13 if edit else 12)
                        self.assertEqual(item[12 if edit else 11], [4])
                    else:
                        # The current website omits OutputSpec for default 720p.
                        self.assertEqual(len(item), 9 if edit else 6)
                        self.assertEqual(spec.resolution, 1)
                    if edit:
                        self.assertEqual(item[0], [None, "video-test", 2, 96])
                    self.assertEqual(rest, original)

    def test_resolution_mismatch_and_unknown_resolution_fail_before_submission(self):
        for model in ABRA_MODELS:
            for resolution in ("VIDEO_RESOLUTION_360P", "VIDEO_RESOLUTION_720P", "VIDEO_RESOLUTION_1080P", "360p"):
                if resolution == MODEL_CONFIG[model]["output_resolution"]:
                    continue
                with self.subTest(model=model, resolution=resolution):
                    rest = video_request(model)
                    rest["requests"][0]["outputSpec"]["resolution"] = resolution
                    with self.assertRaisesRegex(AngularProtocolError, "resolution does not match"):
                        build_video_rpc(rest)

    def test_absent_output_spec_uses_model_resolution_and_aspect_defaults(self):
        for model in ABRA_MODELS:
            with self.subTest(model=model):
                rest = video_request(model)
                del rest["requests"][0]["outputSpec"]
                del rest["requests"][0]["aspectRatio"]
                item = build_video_rpc(rest)[1][0][0]
                self.assertEqual(item[3], 2)
                if model.endswith("_360p"):
                    self.assertEqual(item[-1], [4])
                else:
                    self.assertEqual(len(item), 9 if model.startswith("abra_edit") else 6)

    def test_unverified_aspect_and_output_fields_are_not_silently_dropped(self):
        for field, value in [("aspectRatio", "VIDEO_ASPECT_RATIO_SQUARE"),
                             ("outputSpec", {"resolution": "VIDEO_RESOLUTION_720P", "unverified": True}),
                             ("outputSpec", "720p")]:
            with self.subTest(field=field, value=value):
                rest = video_request("abra_r2v_4s")
                rest["requests"][0][field] = value
                with self.assertRaises(AngularProtocolError):
                    build_video_rpc(rest)

    def test_families_enable_both_resolutions_and_all_catalog_durations(self):
        for model in ABRA_MODELS:
            with self.subTest(model=model):
                family = resolve_video_model(model).family
                self.assertFalse(use_angular_video(model))
                self.assertTrue(use_angular_video(model, families=[family]))
                self.assertFalse(use_angular_video(model, families=["abra_edit" if family == "abra_r2v" else "abra_r2v"]))

    def test_exact_opt_in_is_resolution_specific_and_normalizes_720p_aliases(self):
        for base in [*(f"abra_r2v_{seconds}s" for seconds in (4, 6, 8, 10)), "abra_edit"]:
            with self.subTest(base=base):
                self.assertTrue(use_angular_video(base, models=[base + "_720p"]))
                self.assertTrue(use_angular_video(base + "_720p", models=[base]))
                self.assertTrue(use_angular_video(base + "_360p", models=[base + "_360p"]))
                self.assertFalse(use_angular_video(base, models=[base + "_360p"]))
                self.assertFalse(use_angular_video(base + "_360p", models=[base + "_720p"]))

    def test_unknown_models_and_other_families_are_not_enrolled_by_prefix(self):
        for model in [None, "abra_r2v_5s", "abra_r2v_10s_1080p", "abra_edit_4k",
                      "abra_t2v_4s", "abra_i2v_4s_360p", "omni_flash_i2v_4s_first_last", "abra_edit_future"]:
            with self.subTest(model=model):
                self.assertIsNone(resolve_video_model(model))
                self.assertFalse(use_angular_video(model, families=["abra_r2v", "abra_edit", "abra"]))
        # Preserve previous preflight rejection for an explicitly selected, unsupported model.
        self.assertTrue(use_angular_video("unverified", models=["unverified"]))
        rest = video_request("abra_r2v_4s")
        rest["requests"][0]["videoModelKey"] = "unverified"
        with self.assertRaisesRegex(AngularProtocolError, "not verified"):
            build_video_rpc(rest)

    def test_captured_veo_reference_keeps_own_default_shape(self):
        rest = video_request("abra_r2v_4s")
        rest["requests"][0]["videoModelKey"] = "veo_3_1_r2v_fast_portrait"
        del rest["requests"][0]["aspectRatio"]
        rpc, payload = build_video_rpc(rest)
        self.assertEqual(rpc, "MZZa6b")
        self.assertEqual(len(payload[0][0]), 6)
        self.assertEqual(payload[0][0][3], 1)
        self.assertFalse(use_angular_video("veo_3_1_r2v_fast_portrait", families=["abra_r2v", "abra_edit"]))
        self.assertTrue(use_angular_video("veo_3_1_r2v_fast_portrait", models=["veo_3_1_r2v_fast_portrait"]))

    def test_config_missing_or_invalid_flags_stay_disabled(self):
        settings = Config.__new__(Config)
        for value in [None, "abra_r2v", {"abra_r2v": True}, 1]:
            with self.subTest(value=value):
                settings._config = {"flow": {"angular_video_families": value, "angular_video_models": value}}
                self.assertEqual(settings.flow_angular_video_families, [])
                self.assertEqual(settings.flow_angular_video_models, [])
        settings._config = {}
        self.assertEqual(settings.flow_angular_video_families, [])
        settings._config = {"flow": {"angular_video_families": ["abra_r2v", "abra_edit"]}}
        self.assertEqual(settings.flow_angular_video_families, ["abra_r2v", "abra_edit"])


class AngularFamilyRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = FlowClient(None)
        self.client._make_request = AsyncMock()
        self.client._set_request_fingerprint({"native_token_id": 9})
        self.addCleanup(self.client.clear_request_fingerprint)
        self.settings = SimpleNamespace(captcha_method="native_cdp", flow_angular_video_models=[],
                                        flow_angular_video_families=["abra_r2v", "abra_edit"])
        self.service = SimpleNamespace(fetch_json=AsyncMock())
        for patcher in [patch("src.services.flow_client.config", self.settings),
                        patch("src.services.browser_captcha_native_cdp.BrowserCaptchaService.get_instance",
                              AsyncMock(return_value=self.service))]:
            patcher.start()
            self.addCleanup(patcher.stop)

    async def submit(self, rest):
        edit = rest["requests"][0]["videoModelKey"].startswith("abra_edit")
        return await self.client._make_video_api_request(
            "https://aisandbox-pa.googleapis.com/v1/video:batchAsyncGenerateVideo" + ("EditVideo" if edit else "ReferenceImages"),
            rest, "unused-at", 10,
        )

    async def test_both_resolutions_submit_same_rpc_and_bind_same_account(self):
        row = ["media-test", "project-test", "workflow", "CAE", None, [None] * 8 + [[6]], None, []]
        self.service.fetch_json.return_value = {"rpc_payload": [row]}
        for model in ABRA_MODELS:
            with self.subTest(model=model):
                result = await self.submit(video_request(model))
                call = self.service.fetch_json.await_args.kwargs
                self.assertEqual(call["json_data"]["rpc_id"], resolve_video_model(model).rpc_id)
                self.assertEqual(call["json_data"]["payload"][0][0][2], MODEL_CONFIG[model]["model_key"])
                self.assertEqual(call["token_id"], 9)
                self.assertEqual(call["project_id"], "project-test")
                self.assertTrue(call["consume_video_reservation"])
                self.assertTrue(call["url"].endswith("/data/batchexecute"))
                self.assertNotIn("headers", call)
                self.assertEqual(result["operations"][0]["tokenId"], 9)
                self.assertEqual(result["operations"][0]["transport"], "angular")
        self.client._make_request.assert_not_awaited()

    async def test_explicit_720p_alias_selects_canonical_upstream_key(self):
        self.settings.flow_angular_video_families = []
        self.settings.flow_angular_video_models = ["abra_r2v_4s_720p"]
        row = ["media-test", "project-test", "workflow", "CAE", None, [None] * 8 + [[6]], None, []]
        self.service.fetch_json.return_value = {"rpc_payload": [row]}
        await self.submit(video_request("abra_r2v_4s"))
        self.assertEqual(self.service.fetch_json.await_args.kwargs["json_data"]["rpc_id"], "MZZa6b")

    async def test_disabled_and_unselected_models_keep_native_rest(self):
        self.service.fetch_json.return_value = {"operations": []}
        for model, families, models in [
            ("abra_r2v_4s", [], []),
            ("abra_r2v_4s_360p", [], []),
            ("abra_r2v_4s", [], ["abra_r2v_4s_360p"]),
            ("abra_r2v_4s_360p", [], ["abra_r2v_4s_720p"]),
            ("abra_t2v_4s", ["abra_r2v", "abra_edit"], []),
            ("abra_i2v_4s_360p", ["abra_r2v", "abra_edit"], []),
        ]:
            with self.subTest(model=model, families=families, models=models):
                self.settings.flow_angular_video_families = families
                self.settings.flow_angular_video_models = models
                rest = video_request(model)
                await self.submit(rest)
                call = self.service.fetch_json.await_args.kwargs
                self.assertEqual(call["json_data"], rest)
                self.assertIn("/v1/video:", call["url"])
                self.assertEqual(call["token_id"], 9)
        self.client._make_request.assert_not_awaited()

    async def test_mismatch_fails_without_any_submit_or_rest_fallback(self):
        for model in ABRA_MODELS:
            with self.subTest(model=model):
                rest = video_request(model)
                rest["requests"][0]["outputSpec"]["resolution"] = (
                    "VIDEO_RESOLUTION_720P" if model.endswith("_360p") else "VIDEO_RESOLUTION_360P")
                with self.assertRaises(AngularProtocolError):
                    await self.submit(rest)
        self.service.fetch_json.assert_not_awaited()
        self.client._make_request.assert_not_awaited()

    async def test_uncertain_submit_is_not_replayed_for_either_resolution(self):
        self.service.fetch_json.return_value = {"rpc_payload": []}
        for model in ["abra_r2v_4s", "abra_r2v_4s_360p", "abra_edit", "abra_edit_360p"]:
            with self.subTest(model=model):
                self.service.fetch_json.reset_mock()
                with self.assertRaises(AngularSubmissionUncertain) as captured:
                    await self.submit(video_request(model))
                self.service.fetch_json.assert_awaited_once()
                self.assertFalse(await self.client._handle_retryable_generation_error(
                    captured.exception, 0, 3, "native:9", "project-test", "test"))
        self.client._make_request.assert_not_awaited()
