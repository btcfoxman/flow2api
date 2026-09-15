import base64
import json
import unittest
from unittest.mock import AsyncMock, patch

from src.core.generation_errors import NativeSessionError
from src.core.media_errors import is_media_traffic_error
from src.services.flow_rpc_errors import decode_rpc_status, ERROR_INFO, PUBLIC_AITK_ERROR
from src.services.flow_angular import parse_rpc_response, AngularProtocolError, AngularSubmissionUncertain, AngularRpcRejected
from src.services.browser_captcha_native_cdp import NativeCdpAccountBrowser, BrowserCaptchaService
from src.services.flow_client import FlowClient
from types import SimpleNamespace


def error_frame(code=8, reason=50):
    details = [["type.googleapis.com/" + PUBLIC_AITK_ERROR, [reason]]]
    return json.dumps([["wrb.fr", "MZZa6b", None, None, None, [code, None, details], "generic"]])


class FlowRpcErrorTests(unittest.IsolatedAsyncioTestCase):
    def test_public_error_decodes_json_and_binary_any_without_metadata(self):
        for value in [[50], base64.b64encode(b'\x08\x32').decode()]:
            self.assertEqual(decode_rpc_status([8, None, [["type.googleapis.com/" + PUBLIC_AITK_ERROR, value]]]),
                {'grpc_code': 8, 'public_error': 'PUBLIC_ERROR_UNUSUAL_ACTIVITY_TOO_MUCH_TRAFFIC'})
        name='PUBLIC_ERROR_USER_REQUESTS_THROTTLED'
        encoded=base64.b64encode(b'\x0a'+bytes([len(name)])+name.encode()).decode()
        for value in [[name,'secret domain',{'secret':'cookie'}], encoded]:
            self.assertEqual(decode_rpc_status([8,'secret message',[["type.googleapis.com/"+ERROR_INFO,value]]]),
                {'grpc_code':8,'public_error':name})

    def test_unknown_malformed_and_conflicting_details_fail_closed(self):
        for value in ['%%%','CA==','CA','CAEIAg==',[],[True],[999999],{'secret':'x'}]:
            result=decode_rpc_status([8,None,[["type.googleapis.com/"+PUBLIC_AITK_ERROR,value]]])
            self.assertNotIn('public_error',result)
            self.assertNotIn('secret',str(result))
        self.assertEqual(decode_rpc_status([True]),{})
        self.assertEqual(decode_rpc_status([17]),{})
        self.assertEqual(decode_rpc_status([8,{},[]]),{})
        result=decode_rpc_status([8,None,[[PUBLIC_AITK_ERROR,[49]],[PUBLIC_AITK_ERROR,[2]]]])
        self.assertTrue(result['reason_conflict'])

    def test_definitive_error_is_rejected_not_unknown_but_ambiguous_stays_unknown(self):
        with self.assertRaises(AngularRpcRejected) as caught:
            parse_rpc_response(error_frame(),'MZZa6b')
        self.assertEqual(caught.exception.status_code,429)
        self.assertTrue(is_media_traffic_error(caught.exception))
        self.assertEqual(caught.exception.diagnostics['public_error'],'PUBLIC_ERROR_UNUSUAL_ACTIVITY_TOO_MUCH_TRAFFIC')
        for code in [1,2,4,6,10,13,14,15]:
            with self.subTest(code=code), self.assertRaises(AngularProtocolError) as error:
                parse_rpc_response(error_frame(code),'MZZa6b')
            self.assertNotIsInstance(error.exception,AngularRpcRejected)
        for frames in [json.loads(error_frame())*2,
                       json.loads(error_frame())+[['wrb.fr','MZZa6b','[]']],
                       [['wrb.fr','MZZa6b','[]',None,None,[8]]]]:
            with self.assertRaises(AngularProtocolError) as error:
                parse_rpc_response(json.dumps(frames),'MZZa6b')
            self.assertNotIsInstance(error.exception,AngularRpcRejected)

    async def test_native_mutating_request_preserves_rejection_and_auth_types(self):
        browser=NativeCdpAccountBrowser(98765,None)
        browser._prepare_profile=AsyncMock()
        browser._get_or_create_project_session=AsyncMock(return_value=('target','session'))
        for code, cls, reason in [(8,AngularRpcRejected,'rpc_rejected'),
                                  (16,NativeSessionError,'rpc_rejected'),
                                  (4,AngularSubmissionUncertain,'rpc_response_unrecognized')]:
            browser._evaluate=AsyncMock(return_value={'status':200,'text':error_frame(code)})
            with patch('src.services.browser_captcha_native_cdp.debug_logger.log_runtime_event') as logged:
                with self.assertRaises(cls):
                    await browser.fetch_json(project_id='p',url='https://flow.google.com/_/AiSandboxAngularFrontend/data/batchexecute',
                        json_data={'rpc_id':'MZZa6b','payload':[]})
                rpc_events=[c.kwargs for c in logged.call_args_list if c.args[0]=='native_rpc_failed']
                self.assertEqual(rpc_events[0]['reason'],reason)
                self.assertEqual(rpc_events[0]['grpc_code'],code)
                browser._evaluate.assert_awaited_once()
        self.assertEqual(browser.busy_count,0)

    async def test_explicit_rejection_does_not_retry_inside_flow_client(self):
        client=FlowClient(None)
        client._notify_browser_captcha_error=AsyncMock()
        result=await client._handle_retryable_generation_error(
            AngularRpcRejected({'grpc_code':8,'public_error':'PUBLIC_ERROR_UNUSUAL_ACTIVITY'}),0,3,'native:7','p','test')
        self.assertFalse(result)
        client._notify_browser_captcha_error.assert_not_awaited()

    async def test_http200_business_risk_enters_bound_proxy_cooldown(self):
        service=BrowserCaptchaService(None)
        worker=NativeCdpAccountBrowser(7,None)
        worker.proxy_binding=SimpleNamespace(url='socks5://127.0.0.1:20019')
        service._workers[7]=worker
        service._ensure_capacity=AsyncMock()
        service._record_proxy_risk=AsyncMock()
        worker.fetch_json=AsyncMock(side_effect=AngularRpcRejected({'grpc_code':8,
            'public_error':'PUBLIC_ERROR_UNUSUAL_ACTIVITY_TOO_MUCH_TRAFFIC'}))
        try:
            with self.assertRaises(AngularRpcRejected):
                await service.fetch_json(token_id=7,project_id='p',
                    url='https://flow.google.com/_/AiSandboxAngularFrontend/data/batchexecute',
                    json_data={'rpc_id':'MZZa6b','payload':[]})
            service._record_proxy_risk.assert_awaited_once()
            self.assertEqual(service._record_proxy_risk.await_args.kwargs['token_proxy_url'],worker.proxy_binding.url)
            self.assertEqual(worker.busy_count,0)
        finally:
            await service.close()

    def test_quota_and_policy_do_not_quarantine_the_proxy(self):
        for reason, expected in [('PUBLIC_ERROR_USER_QUOTA_REACHED',503),
                                 ('PUBLIC_ERROR_PER_MODEL_DAILY_QUOTA_REACHED',403),
                                 ('PUBLIC_ERROR_UNSAFE_IMAGE_UPLOAD',400)]:
            error=AngularRpcRejected({'grpc_code':8,'public_error':reason})
            self.assertEqual(error.status_code,expected)
            self.assertFalse(is_media_traffic_error(error))

    def test_real_media_status_preserves_policy_reason_and_true_error_code(self):
        from src.services.flow_angular import video_operations
        from src.core.media_errors import media_generation_failure_response
        media=['m','p','w','CAE',None,[None]*8+[[4,[3,'PUBLIC_ERROR_UNSAFE_GENERATION: private prompt'],['private']]],None,[]]
        error=video_operations([media],token_id=1,project_id='p')['operations'][0]['operation']['error']
        self.assertEqual(error['wire_status'],4)
        self.assertEqual(error['code'],3)
        self.assertEqual(error['code_source'],'google_rpc_status')
        self.assertNotIn('private',str(error))
        self.assertEqual(media_generation_failure_response('video',error['message'])[1],400)
