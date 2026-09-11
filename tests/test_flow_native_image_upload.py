"""Wire regressions from g9-9urm manual create/upload/NARWHAL on 2026-09-11.

All credentials, bytes, prompts and account/media identifiers are synthetic.
"""
import asyncio
import base64
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.core.config import config
from src.core.generation_errors import NativeSessionError
from src.services.flow_client import FlowClient
from src.services.browser_captcha_native_cdp import NativeCdpAccountBrowser, BrowserCaptchaService
from src.services.flow_angular import (AngularProtocolError, AngularSubmissionUncertain,
    IMAGE_ASPECT_RATIOS, build_create_project_rpc, created_project, build_image_upload_rpc,
    uploaded_image_id, build_image_rpc, image_result, project_context)

PROJECT = "test-project"
MEDIA = "test-upload"
WORKFLOW = "test-workflow"
UPLOAD_RESPONSE = [[MEDIA,PROJECT,WORKFLOW,"CAE",None,[],[None,None,[864,1152]]],
                   [WORKFLOW,None,None,["sample.png"],PROJECT]]
PNG = b"\x89PNG\r\n\x1a\n" + b"test-not-a-real-image"


class CapturedImageWireTests(unittest.TestCase):
    def test_project_creation_shape_and_response(self):
        rpc,payload=build_create_project_rpc("Flow test")
        self.assertEqual(rpc,"jHPbke")
        self.assertEqual(payload,["projects/*",[None,["Flow test"]],[None,22]])
        self.assertEqual(created_project([PROJECT,["Flow test"]]),{"project_id":PROJECT,"project_name":"Flow test"})
        for bad in ([], {}, ["../other",["title"]], [None,["title"]]):
            with self.assertRaises(AngularSubmissionUncertain): created_project(bad)

    def test_upload_shape_uses_upload_captcha_not_oauth(self):
        with patch("src.services.flow_angular.uuid.uuid4",side_effect=["seed-a","seed-b"]):
            rpc,payload=build_image_upload_rpc(PROJECT,"fresh-upload",PNG,"image/png","sample.png")
        self.assertEqual(rpc,"maseQ")
        self.assertEqual(payload,[project_context(PROJECT,"fresh-upload"),base64.b64encode(PNG).decode(),
            "image/png",1,None,None,None,None,"sample.png",None,"SEED-A","SEED-B"])
        self.assertEqual(uploaded_image_id(UPLOAD_RESPONSE,PROJECT),MEDIA)

    def test_upload_requires_both_media_and_workflow_project_binding(self):
        for change in ("media-project","workflow-project","workflow-id","shape"):
            import copy
            reply=copy.deepcopy(UPLOAD_RESPONSE)
            if change=="media-project":reply[0][1]="foreign"
            elif change=="workflow-project":reply[1][4]="foreign"
            elif change=="workflow-id":reply[1][0]="other"
            else:reply[0]=[]
            with self.assertRaises(AngularSubmissionUncertain):uploaded_image_id(reply,PROJECT)

    def test_upload_rejects_invalid_input_before_rpc(self):
        for image,mime,filename,captcha in [(b"","image/png","x.png","captcha"),
                (PNG,"text/plain","x.txt","captcha"),(PNG,"image/png","../x.png","captcha"),
                (PNG,"image/png","x.png","")]:
            with self.assertRaises(AngularProtocolError):build_image_upload_rpc(PROJECT,captcha,image,mime,filename)

    def test_narwhal_and_gem_pix_use_flat_batch_context_and_reference_id(self):
        for model in ("NARWHAL","GEM_PIX_2"):
            for aspect,enum in IMAGE_ASPECT_RATIOS.items():
                req={"clientContext":{"projectId":PROJECT,"recaptchaContext":{"token":"fresh-generation"}},
                    "mediaGenerationContext":{"batchId":"test-batch"},"requests":[{"imageModelName":model,
                        "imageAspectRatio":aspect,"seed":123,"structuredPrompt":{"parts":[{"text":"test prompt"}]},
                        "imageInputs":[{"name":MEDIA,"imageInputType":"IMAGE_INPUT_TYPE_REFERENCE"}]}]}
                rpc,payload=build_image_rpc(req)
                self.assertEqual(rpc,"ogiZ0b")
                self.assertEqual(payload[4],["test-batch"])
                entry=payload[1][0]
                self.assertEqual(entry[2],[[MEDIA,None,None,None,1]])
                self.assertEqual(entry[3:6],[123,enum,model])
                self.assertEqual(entry[7],project_context(PROJECT,"fresh-generation"))
                self.assertEqual(entry[8],[[["test prompt"]]])


class NativeImageTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        previous=config.captcha_method
        config.set_captcha_method("native_cdp")
        self.addCleanup(config.set_captcha_method,previous)
        self.client=FlowClient(None,SimpleNamespace(get_token=AsyncMock(return_value=SimpleNamespace(auth_mode="flow"))))
        self.service=SimpleNamespace(get_token=AsyncMock(return_value=("fresh-upload","native:7")),
            fetch_json=AsyncMock(return_value={"rpc_payload":UPLOAD_RESPONSE}))
        manager=patch("src.services.browser_captcha_native_cdp.BrowserCaptchaService.get_instance",AsyncMock(return_value=self.service))
        manager.start()
        self.addCleanup(manager.stop)

    async def test_upload_uses_native_project_and_upload_action_without_labs(self):
        self.client.ensure_flow_image_upload_acknowledgement=AsyncMock(side_effect=AssertionError("no Labs"))
        self.client._make_request=AsyncMock(side_effect=AssertionError("no OAuth"))
        result=await self.client.upload_image(None,PNG,project_id=PROJECT,token_id=7,st="unused",model_key="abra_r2v_4s_360p")
        self.assertEqual(result,MEDIA)
        self.service.get_token.assert_awaited_once_with(PROJECT,action="UPLOAD_IMAGE",token_id=7,page_protocol="angular")
        sent=self.service.fetch_json.await_args.kwargs
        self.assertEqual(sent["token_id"],7)
        self.assertEqual(sent["project_id"],PROJECT)
        self.assertEqual(sent["json_data"]["rpc_id"],"maseQ")
        self.assertNotIn("headers",sent)
        self.client.ensure_flow_image_upload_acknowledgement.assert_not_awaited()
        self.client._make_request.assert_not_awaited()

    async def test_missing_upload_captcha_never_submits(self):
        self.service.get_token.return_value=(None,None)
        with self.assertRaises(NativeSessionError):
            await self.client.upload_image(None,PNG,project_id=PROJECT,token_id=7)
        self.service.fetch_json.assert_not_awaited()

    async def test_uncertain_upload_is_not_retried_or_converted_to_legacy(self):
        self.service.fetch_json.side_effect=AngularSubmissionUncertain("unknown")
        with self.assertRaises(AngularSubmissionUncertain):
            await self.client.upload_image(None,PNG,project_id=PROJECT,token_id=7)
        self.service.fetch_json.assert_awaited_once()

    async def test_project_creation_uses_root_native_page_once(self):
        self.service.fetch_json.return_value={"rpc_payload":[PROJECT,["Test"]]}
        result=await self.client.create_flow_project(7,"Test")
        self.assertEqual(result["project_id"],PROJECT)
        sent=self.service.fetch_json.await_args.kwargs
        self.assertEqual(sent["project_id"],"")
        self.assertEqual(sent["json_data"]["rpc_id"],"jHPbke")
        self.assertNotIn("headers",sent)
        self.service.get_token.assert_not_awaited()

    async def test_browser_mutation_timeouts_and_bad_frames_are_uncertain(self):
        browser=NativeCdpAccountBrowser(98765,None)
        browser._prepare_profile=AsyncMock()
        browser._get_or_create_project_session=AsyncMock(return_value=("target","session"))
        for rpc in ("maseQ","jHPbke"):
            for failure in (asyncio.TimeoutError(),{"status":200,"text":"unrecognized response"}):
                browser._evaluate=AsyncMock(side_effect=failure) if isinstance(failure,Exception) else AsyncMock(return_value=failure)
                with self.assertRaises(AngularSubmissionUncertain):
                    await browser.fetch_json(project_id=PROJECT,
                        url="https://flow.google.com/_/AiSandboxAngularFrontend/data/batchexecute",
                        json_data={"rpc_id":rpc,"payload":[]})
                browser._evaluate.assert_awaited_once()

    async def test_rpc_failures_retain_safe_diagnostics_and_429_classification(self):
        from src.core.media_errors import is_media_traffic_error, media_generation_failure_response
        browser=NativeCdpAccountBrowser(98765,None)
        browser._prepare_profile=AsyncMock()
        browser._get_or_create_project_session=AsyncMock(return_value=("target","session"))
        for response, reason, expected in [
            ({"status":429,"text":"secret upstream body"}, "rpc_http_rejected", AngularProtocolError),
            ({"status":200,"text":"malformed secret body"}, "rpc_response_unrecognized", AngularSubmissionUncertain),
            ({"fetchError":"secret network details"}, "rpc_fetch_interrupted", AngularSubmissionUncertain),
            (asyncio.TimeoutError(), "rpc_cdp_timeout", AngularSubmissionUncertain),
        ]:
            browser._evaluate=AsyncMock(side_effect=response) if isinstance(response, Exception) else AsyncMock(return_value=response)
            with patch("src.services.browser_captcha_native_cdp.debug_logger.log_runtime_event") as logged:
                with self.assertRaises(expected) as caught:
                    await browser.fetch_json(project_id=PROJECT,
                        url="https://flow.google.com/_/AiSandboxAngularFrontend/data/batchexecute",
                        json_data={"rpc_id":"ogiZ0b","payload":["secret captcha"]})
                fields=logged.call_args.kwargs
                self.assertEqual(fields["reason"],reason)
                self.assertEqual(fields["rpc_id"],"ogiZ0b")
                self.assertIn("duration_ms",fields)
                self.assertNotIn("secret",str(fields))
                if reason == "rpc_http_rejected":
                    self.assertTrue(is_media_traffic_error(caught.exception))
                    self.assertEqual(media_generation_failure_response("image",caught.exception)[1],429)
                    self.assertEqual(fields["status_code"],429)

    async def test_image_rpc_records_egress_risk_without_video_reservation(self):
        service=BrowserCaptchaService(None)
        worker=NativeCdpAccountBrowser(7,None)
        worker.proxy_binding=SimpleNamespace(url="socks5://127.0.0.1:20019")
        service._workers[7]=worker
        service._ensure_capacity=AsyncMock()
        service._record_proxy_risk=AsyncMock()
        service._record_proxy_success=AsyncMock()
        try:
            worker.fetch_json=AsyncMock(side_effect=AngularProtocolError("Flow RPC rejected: HTTP Error 429"))
            with self.assertRaises(AngularProtocolError):
                await service.fetch_json(token_id=7,project_id=PROJECT,
                    url="https://flow.google.com/_/AiSandboxAngularFrontend/data/batchexecute",
                    json_data={"rpc_id":"ogiZ0b","payload":[]})
            service._record_proxy_risk.assert_awaited_once()
            self.assertEqual(service._record_proxy_risk.await_args.kwargs["token_proxy_url"],worker.proxy_binding.url)
            self.assertEqual(worker.busy_count,0)
            self.assertEqual(worker._video_submit_reservations,[])
            worker.fetch_json=AsyncMock(return_value={"rpc_payload":[]})
            await service.fetch_json(token_id=7,project_id=PROJECT,
                url="https://flow.google.com/_/AiSandboxAngularFrontend/data/batchexecute",
                json_data={"rpc_id":"ogiZ0b","payload":[]})
            service._record_proxy_success.assert_awaited_once()
        finally:
            await service.close()
