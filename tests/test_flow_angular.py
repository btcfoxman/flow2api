import json
import unittest
import base64
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.core.config import config
from src.core.media_errors import sanitize_public_error_message
from src.services.flow_client import FlowClient
from src.services.browser_captcha_native_cdp import NativeCdpAccountBrowser
from src.services.flow_angular import (parse_rpc_response, video_operations, build_video_rpc,
    AngularProtocolError, AngularSubmissionUncertain)


def media(status, media_id="media-test"):
    rendition = [None] * 8 + [f"https://flow-content.google/video/{media_id}?Expires=9999999999&KeyName=test&Signature=test"]
    return [media_id, "project-test", "workflow", "CAE", None, [None] * 8 + [[status]], None, [rendition]]


class AngularCodecTests(unittest.TestCase):
    def test_frames_multibyte_text_and_nested_json(self):
        payload = [None, 46, [media(6)]]
        frame = json.dumps([["wrb.fr", "MZZa6b", json.dumps(payload)], ["di", 123]], ensure_ascii=False)
        raw = ")]}'\n\n" + str(len(frame.encode())) + "\n" + frame + '\n26\n[["e",4,null,null,1027]]\n'
        self.assertEqual(parse_rpc_response(raw, "MZZa6b"), payload)
        self.assertEqual(parse_rpc_response('[["wrb.fr","x","[\\"你好\\"]"]]', "x"), ["你好"])

    def test_malformed_missing_error_or_duplicate_rpc_is_rejected(self):
        for raw in ['<html>login</html>', '[["wrb.fr","x",null,null,13]]', '[["wrb.fr","other","[]"]]',
                    '[["wrb.fr","x","[]"],["wrb.fr","x","[]"]]']:
            with self.subTest(raw=raw), self.assertRaises(AngularProtocolError):
                parse_rpc_response(raw, "x")

    def test_only_confirmed_success_and_matching_media_counts(self):
        for code, suffix in [(6,"PENDING"),(2,"ACTIVE"),(3,"SUCCESSFUL")]:
            response = video_operations([media(code)], token_id=9, project_id="project-test")
            op = response["operations"][0]
            self.assertTrue(op["status"].endswith(suffix))
            self.assertEqual(op["tokenId"], 9)
            self.assertEqual(op["transport"], "angular")
        with self.assertRaises(AngularProtocolError):
            video_operations([media(99)], token_id=9, project_id="project-test")
        with self.assertRaises(AngularProtocolError):
            video_operations([media(3)], token_id=9, project_id="another-project")
        with self.assertRaises(AngularProtocolError):
            video_operations([media(3)], token_id=9, project_id="project-test", expected_ids=["missing"])

    def test_model_specific_wire_tail_and_unverified_models(self):
        rest = {"clientContext":{"projectId":"project-test", "recaptchaContext":{"token":"captcha"}},
                "requests":[{"videoModelKey":"abra_r2v_4s_360p", "textInput":{"structuredPrompt":{"parts":[{"text":"prompt"}]}},
                             "referenceImages":[{"mediaId":"image-test"}]}]}
        rpc, payload = build_video_rpc(rest)
        self.assertEqual(rpc, "MZZa6b")
        self.assertEqual(payload[0][0][-1], [4])
        self.assertEqual(payload[1][5], "project-test")
        rest["requests"][0]["videoModelKey"] = "veo_3_1_r2v_fast_portrait"
        self.assertEqual(len(build_video_rpc(rest)[1][0][0]), 6)
        rest["requests"][0]["videoModelKey"] = "abra_r2v_4s_1080p"
        with self.assertRaises(AngularProtocolError):
            build_video_rpc(rest)

    def test_never_returns_unsigned_or_foreign_video_urls(self):
        for url in ["https://flow-content.google/video/media-test", "https://evil.test/video/media-test?Expires=1&KeyName=k&Signature=s"]:
            row = media(3)
            row[7][0][8] = url
            result = video_operations([row], token_id=1, project_id="project-test")
            self.assertNotIn("fifeUrl", result["operations"][0]["operation"]["metadata"]["video"])

    def test_uncertain_submission_has_a_safe_public_message(self):
        text = sanitize_public_error_message("Flow launch result is unconfirmed; automatic resubmission is disabled")
        self.assertIn("请勿立即重复提交", text)
        self.assertNotIn("Flow", text)


class AngularRoutingTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is required to execute the browser upload script")
    async def test_upload_browser_script_uses_google_resumable_protocol(self):
        worker = NativeCdpAccountBrowser(1, None)
        async def evaluate(_session, expression, **_kwargs):
            harness = """
const fs = require('fs');
global.location = {origin:'https://flow.google.com'};
const calls = [];
global.fetch = async (url, options) => {
  calls.push({url, method:options.method, headers:options.headers, length:options.body?.length || 0});
  if (calls.length === 1) return {ok:true,headers:{get:key=>key==='x-goog-upload-url' ? 'https://flow.google.com/upload/v1/flow/upload/video/p?upload_id=test' : '1048576'}};
  return {ok:true,json:async()=>({mediaId:'uploaded',workflow:{name:'workflow'}})};
};
eval(fs.readFileSync(0,'utf8')).then(result=>process.stdout.write(JSON.stringify({result,calls})));
"""
            result = subprocess.run([shutil.which("node"), "-e", harness], input=expression, capture_output=True, text=True, timeout=10, check=True)
            parsed = json.loads(result.stdout)
            self.assertEqual(len(parsed["calls"]), 3)
            self.assertEqual(parsed["calls"][0]["headers"]["x-goog-upload-command"], "start")
            self.assertEqual(parsed["calls"][1]["headers"]["x-goog-upload-command"], "upload")
            self.assertEqual(parsed["calls"][2]["headers"]["x-goog-upload-command"], "upload, finalize")
            self.assertEqual(parsed["calls"][2]["headers"]["x-goog-upload-offset"], "1048576")
            self.assertTrue(all(call["method"] == "POST" for call in parsed["calls"]))
            return parsed["result"]
        worker._evaluate = evaluate
        result = await worker._upload_flow_video("s", "https://flow.google.com/upload/v1/flow/upload/video/p",
            {"video_base64":base64.b64encode(b"x" * 1048577).decode()}, 10)
        self.assertEqual(result["mediaServerId"], "uploaded")
        self.assertEqual(result["transport"], "angular")

    async def test_unknown_submit_outcome_is_never_retried(self):
        client = FlowClient(None)
        client._notify_browser_captcha_error = AsyncMock()
        self.assertFalse(await client._handle_retryable_generation_error(AngularSubmissionUncertain("unconfirmed"), 0, 3, "native:1", "p", "test"))
        client._notify_browser_captcha_error.assert_not_awaited()

    async def test_polling_uses_persisted_account_binding(self):
        client = FlowClient(None)
        service = SimpleNamespace(fetch_json=AsyncMock(return_value={"rpc_payload":[None,None,[media(3)]]}))
        ops = video_operations([media(6)], token_id=9, project_id="project-test")["operations"]
        with patch("src.services.browser_captcha_native_cdp.BrowserCaptchaService.get_instance", AsyncMock(return_value=service)):
            result = await client.check_video_status("unused-at", ops)
        self.assertEqual(service.fetch_json.await_args.kwargs["token_id"], 9)
        self.assertTrue(result["operations"][0]["status"].endswith("SUCCESSFUL"))

    async def test_polling_hydrates_missing_signed_url_without_resubmitting(self):
        row = media(3)
        row[7] = []
        service = SimpleNamespace(fetch_json=AsyncMock(side_effect=[{"rpc_payload":[row]}, {"rpc_payload":media(3)}]))
        ops = video_operations([media(6)], token_id=9, project_id="project-test")["operations"]
        with patch("src.services.browser_captcha_native_cdp.BrowserCaptchaService.get_instance", AsyncMock(return_value=service)):
            result = await FlowClient(None).check_video_status("unused", ops)
        self.assertIn("Signature=", result["operations"][0]["operation"]["metadata"]["video"]["fifeUrl"])
        self.assertEqual([c.kwargs["json_data"]["rpc_id"] for c in service.fetch_json.await_args_list], ["jwpduf", "as29s"])
