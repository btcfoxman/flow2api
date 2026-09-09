import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.core.config import config
from src.core.generation_errors import NativeSessionError, is_upstream_authentication_error
from src.core.media_errors import sanitize_public_error_message
from src.services.browser_captcha_native_cdp import NativeCdpAccountBrowser
from src.services.flow_client import FlowClient
from src.services.generation_handler import GenerationHandler


class VideoPollAuthRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def test_auth_classification_does_not_match_ids(self):
        self.assertTrue(is_upstream_authentication_error('Flow API request failed: HTTP Error 401: secret'))
        self.assertFalse(is_upstream_authentication_error('task-401 failed'))
        self.assertNotIn('secret', sanitize_public_error_message('HTTP Error 401: secret'))
        self.assertFalse(GenerationHandler.__new__(GenerationHandler)._should_record_token_error('HTTP Error 401: secret', 502))

    async def test_poll_401_does_not_retry_another_payload_with_same_at(self):
        client = FlowClient(None)
        client._make_video_api_request = AsyncMock(side_effect=RuntimeError('HTTP Error 401: secret'))
        with self.assertRaises(NativeSessionError) as caught:
            await client.check_video_status('old', [{'operation': {'name': 'task'}, 'mediaName': 'media', 'projectId': 'project'}])
        self.assertEqual(client._make_video_api_request.await_count, 1)
        self.assertNotIn('secret', str(caught.exception))

    async def test_rest_and_rpc_401_remain_typed(self):
        for url, body in [('https://aisandbox-pa.googleapis.com/v1/video:batchAsyncGenerateVideo', {}),
                          ('https://flow.google.com/_/AiSandboxAngularFrontend/data/batchexecute', {'rpc_id': 'jwpduf', 'payload': []})]:
            browser = NativeCdpAccountBrowser(1, None)
            browser._prepare_profile = AsyncMock()
            browser._get_or_create_project_session = AsyncMock(return_value=('target', 'session'))
            browser._evaluate = AsyncMock(return_value={'status': 401, 'text': 'private upstream details'})
            with self.assertRaises(NativeSessionError) as caught:
                await browser.fetch_json(project_id='project', url=url, json_data=body)
            self.assertEqual(caught.exception.reason, 'upstream_authentication_rejected')
            self.assertEqual(browser._evaluate.await_count, 1)
            self.assertEqual(browser.busy_count, 0)

    def handler(self):
        handler = GenerationHandler.__new__(GenerationHandler)
        handler.db = SimpleNamespace(get_token=AsyncMock(return_value=SimpleNamespace(id=7, at='new-at', st='new-st')))
        handler.flow_client = SimpleNamespace(check_video_status=AsyncMock())
        handler._update_request_log_progress = AsyncMock()
        handler._finalize_async_video_result_log = AsyncMock()
        handler._complete_video_task = AsyncMock()
        handler._fail_video_task = AsyncMock()
        handler.watermark_processor = SimpleNamespace(apply_policy=AsyncMock(return_value='https://example.test/video'))
        handler.file_cache = object()
        handler._get_base_url = lambda _: 'http://localhost'
        return handler

    async def test_same_account_refresh_recovers_after_more_than_three_401s(self):
        handler = self.handler()
        handler.flow_client.check_video_status.side_effect = [NativeSessionError('upstream_authentication_rejected')] * 3 + [
            {'operations': [{'operation': {'name': 'task', 'metadata': {'video': {'fifeUrl': 'https://example.test/video'}}},
                             'status': 'MEDIA_GENERATION_STATUS_SUCCESSFUL'}]}]
        result = {}
        with patch.dict(config._config, {'flow': {**config._config['flow'], 'max_poll_attempts': 4, 'poll_interval': 1}}), \
             patch('src.services.generation_handler.asyncio.sleep', AsyncMock()):
            chunks = [chunk async for chunk in handler._poll_video_result(
                SimpleNamespace(id=7, at='old', st='old'), 'project', [{'operation': {'name': 'task'}}], False,
                generation_result=result)]
        self.assertTrue(result['success'])
        self.assertEqual(handler.flow_client.check_video_status.await_count, 4)
        self.assertTrue(all(call.args[0] == 'new-at' for call in handler.flow_client.check_video_status.await_args_list))
        handler._fail_video_task.assert_not_awaited()
        handler._complete_video_task.assert_awaited_once()

    async def test_expired_poll_window_reports_unknown_result_not_resubmission(self):
        handler = self.handler()
        handler.flow_client.check_video_status.side_effect = NativeSessionError('upstream_authentication_rejected')
        result = {}
        operations = [{'operation': {'name': 'original-task'}}]
        with patch.dict(config._config, {'flow': {**config._config['flow'], 'max_poll_attempts': 4, 'poll_interval': 1}}), \
             patch('src.services.generation_handler.asyncio.sleep', AsyncMock()):
            chunks = [chunk async for chunk in handler._poll_video_result(
                SimpleNamespace(id=7, at='old', st='old'), 'project', operations, False, generation_result=result)]
        public = json.loads(chunks[-1])['error']
        self.assertEqual(public['status_code'], 503)
        self.assertIn('勿重复提交', public['message'])
        self.assertNotIn('authentication', public['message'])
        self.assertFalse(handler._should_record_token_error(result['error_message'], 503))
        self.assertEqual(handler._fail_video_task.await_args.args[0], operations)

    async def test_non_streaming_task_reports_poll_and_postprocessing_stages(self):
        handler = self.handler()
        handler.flow_client.check_video_status.return_value = {
            'operations': [{'operation': {'name': 'task', 'metadata': {
                'video': {'fifeUrl': 'https://example.test/source'}}},
                'status': 'MEDIA_GENERATION_STATUS_SUCCESSFUL'}]}
        async def postprocess(**kwargs):
            self.assertEqual(handler._update_request_log_progress.await_args.kwargs['status_text'],
                             'video_postprocessing')
            return 'https://example.test/processed'
        handler.watermark_processor.apply_policy.side_effect = postprocess
        result = {}
        with patch('src.services.generation_handler.asyncio.sleep', AsyncMock()):
            chunks = [chunk async for chunk in handler._poll_video_result(
                SimpleNamespace(id=7, at='at', st='st'), 'project',
                [{'operation': {'name': 'task'}}], False, generation_result=result)]
        stages = [call.kwargs['status_text'] for call in handler._update_request_log_progress.await_args_list]
        self.assertEqual(stages, ['video_polling', 'video_postprocessing'])
        self.assertTrue(result['success'])
        self.assertEqual(len(chunks), 1)
