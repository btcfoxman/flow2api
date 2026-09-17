"""Captured native video/image parity. Offline: never submits a paid request."""
import asyncio
import copy
import io
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from PIL import Image

from src.core.credits import is_quota_exhausted_error
from src.core.model_resolver import resolve_model_name
from src.services.browser_captcha_native_cdp import NativeCdpAccountBrowser
from src.services.flow_angular import (
    AngularProtocolError, AngularRpcRejected, AngularSubmissionUncertain,
    FLOW_RPC_URL, VIDEO_GENERATION_RPC_IDS, MUTATING_RPC_IDS,
    build_image_rpc, build_video_rpc, image_result, parse_rpc_response,
    resolve_video_model, use_angular_video, video_frame, video_frame_crop, video_operations,
)
from src.services.flow_client import FlowClient
from src.services.generation_handler import MODEL_CONFIG, GenerationHandler
from src.services.model_capabilities import supports_flow_model


FIXTURE = json.loads((Path(__file__).parent / 'fixtures/flow_video_20260917.json').read_text(encoding='utf-8'))
VIDEO_CASES = ('fast_portrait', 'lite_landscape', 'first_last_360p')


def request(case):
    wire, context, batch = case['request']
    item = wire[0]
    frames = case['rpc_id'] == 'nprQif'
    model, aspect = (item[1], item[2]) if frames else (item[2], item[3])
    data = {
        'videoModelKey': model,
        'aspectRatio': 'VIDEO_ASPECT_RATIO_PORTRAIT' if aspect == 1 else 'VIDEO_ASPECT_RATIO_LANDSCAPE',
        'textInput': {'structuredPrompt': {'parts': [{'text': item[0][2][0][0][0]}]}},
        'outputSpec': {'resolution': 'VIDEO_RESOLUTION_360P' if model.endswith('_360p') else 'VIDEO_RESOLUTION_720P'},
    }
    if frames:
        for key, row in (('startImage', item[4]), ('endImage', item[5])):
            data[key] = {'mediaId': row[1], 'cropCoordinates': dict(zip(
                ('top', 'left', 'bottom', 'right'), [v or 0 for v in row[5]]))}
    else:
        data['referenceImages'] = [{'mediaId': ref[1]} for ref in item[1]]
    return {'requests': [data], 'clientContext': {'projectId': context[5],
            'recaptchaContext': {'token': context[10][0]}}, 'mediaGenerationContext': {'batchId': batch[0]}}


def rpc_text(rpc, payload):
    frame = json.dumps([['wrb.fr', rpc, json.dumps(payload), None, None, None, 'generic']])
    return ")]}'\n\n" + str(len(frame.encode())) + '\n' + frame


class CapturedVideoWireTests(unittest.TestCase):
    def test_exact_video_request_parity(self):
        for name in (*VIDEO_CASES, 'quota'):
            case = FIXTURE[name]
            rest = request(case)
            before = copy.deepcopy(rest)
            item = case['request'][0][0]
            metadata = item[6] if name == 'first_last_360p' else item[5]
            with self.subTest(name=name), patch('src.services.flow_angular.uuid.uuid4', side_effect=metadata[4:6]):
                rpc, payload = build_video_rpc(rest)
                self.assertEqual(rpc, case['rpc_id'])
                self.assertEqual(payload, case['request'])
                self.assertEqual(rest, before)

    def test_catalog_backed_variants_not_unbounded_prefixes(self):
        for row in FIXTURE['catalog']:
            model = row['model']
            if model.endswith('_low_priority'):
                self.assertIsNone(resolve_video_model(model))
                continue
            spec = resolve_video_model(model)
            self.assertIsNotNone(spec)
            self.assertIn(spec.resolution, row['resolutions'])
            for aspect in row['aspects']:
                rest = request(FIXTURE['first_last_360p' if model.startswith('omni_') else 'lite_landscape'])
                data = rest['requests'][0]
                data['videoModelKey'] = model
                data['aspectRatio'] = 'VIDEO_ASPECT_RATIO_PORTRAIT' if aspect == 1 else 'VIDEO_ASPECT_RATIO_LANDSCAPE'
                data['outputSpec']['resolution'] = 'VIDEO_RESOLUTION_360P' if spec.resolution == 4 else 'VIDEO_RESOLUTION_720P'
                with self.subTest(model=model, aspect=aspect):
                    rpc, payload = build_video_rpc(rest)
                    self.assertEqual(rpc, spec.rpc_id)
                    self.assertEqual(len(payload[0][0]), 7 if model.startswith('omni_') else 6)
                    self.assertFalse(use_angular_video(model, families=['abra_r2v', 'abra_t2v']))
                    self.assertTrue(use_angular_video(model, models=[model]))
        for model in ('omni_flash_i2v_5s_first_last', 'omni_flash_i2v_4s_first_last_1080p',
                      'veo_3_1_r2v_fast_landscape_ultra_relaxed', 'veo_3_1_t2v_fast', 'veo_3_1_i2v_s_fast'):
            self.assertIsNone(resolve_video_model(model))
        self.assertEqual(resolve_video_model('omni_flash_i2v_4s_first_last_720p'),
                         resolve_video_model('omni_flash_i2v_4s_first_last'))

    def test_public_lite_aliases_and_capabilities(self):
        for aspect in ('landscape', 'portrait'):
            name = resolve_model_name('veo_3_1_r2v_lite', request=SimpleNamespace(
                generationConfig=SimpleNamespace(aspectRatio=aspect)), model_config=MODEL_CONFIG)
            self.assertEqual(name, 'veo_3_1_r2v_lite_' + aspect)
            cfg = MODEL_CONFIG[name]
            self.assertTrue(supports_flow_model(cfg))
            self.assertEqual((cfg['min_images'], cfg['max_images']), (1, 3))
            self.assertEqual(cfg['model_key'], 'veo_3_1_r2v_lite')
            handler = GenerationHandler.__new__(GenerationHandler)
            self.assertEqual(handler._resolve_video_model_key_for_tier(cfg, 'PAYGATE_TIER_TWO')[0], cfg['model_key'])
        self.assertTrue(supports_flow_model(MODEL_CONFIG['veo_3_1_r2v_fast']))
        self.assertTrue(supports_flow_model(MODEL_CONFIG['veo_3_1_r2v_fast_ultra']))
        self.assertFalse(supports_flow_model(MODEL_CONFIG['veo_3_1_r2v_fast_ultra_4k']))
        self.assertFalse(supports_flow_model(MODEL_CONFIG['veo_3_1_r2v_fast_portrait_ultra_1080p']))

    def test_invalid_inputs_fail_before_submit(self):
        mutations = [
            ('first_last_360p', 'endImage', None),
            ('first_last_360p', 'referenceImages', [{'mediaId': 'ref'}]),
            ('first_last_360p', 'outputSpec', {'resolution': 'VIDEO_RESOLUTION_720P'}),
            ('fast_portrait', 'aspectRatio', 'VIDEO_ASPECT_RATIO_LANDSCAPE'),
            ('lite_landscape', 'referenceImages', []),
            ('lite_landscape', 'referenceImages', [{'mediaId': 'ref'}] * 4),
            ('lite_landscape', 'referenceImages', [None]),
            ('lite_landscape', 'startImage', {'mediaId': 'frame'}),
        ]
        for name, key, value in mutations:
            rest = request(FIXTURE[name])
            rest['requests'][0][key] = value
            with self.subTest(name=name, key=key), self.assertRaises(AngularProtocolError):
                build_video_rpc(rest)

    def test_crop_parity_bounds_and_orientation(self):
        def image_bytes(size, orientation=None):
            image = Image.new('RGB', size)
            out = io.BytesIO()
            exif = Image.Exif()
            if orientation:
                exif[274] = orientation
            image.save(out, format='PNG', exif=exif)
            return out.getvalue()
        landscape = 'VIDEO_ASPECT_RATIO_LANDSCAPE'
        self.assertEqual(video_frame_crop(image_bytes((1024, 1024)), landscape),
                         {'top': 0.21875, 'left': 0, 'bottom': 0.78125, 'right': 1})
        self.assertEqual(video_frame_crop(image_bytes((1376, 768)), landscape),
                         {'top': 0, 'left': 0, 'bottom': 1, 'right': 1})
        self.assertEqual(video_frame_crop(image_bytes((768, 1376), 6), landscape),
                         {'top': 0, 'left': 0, 'bottom': 1, 'right': 1})
        crop = video_frame_crop(image_bytes((1024, 1024)), 'VIDEO_ASPECT_RATIO_PORTRAIT')
        self.assertEqual(crop, {'top': 0, 'left': 0.21875, 'bottom': 1, 'right': 0.78125})
        self.assertEqual(video_frame({'mediaId': 'test'})[-1], [None, None, 1, 1])
        for value in (True, -1, float('nan'), float('inf'), 1, '0'):
            with self.subTest(value=value), self.assertRaises(AngularProtocolError):
                video_frame({'mediaId': 'test', 'cropCoordinates': {'top': value, 'left': 0, 'bottom': 1, 'right': 1}})
        with self.assertRaises(AngularProtocolError):
            video_frame_crop(b'not an image', landscape)

    def test_complete_requires_detail_to_get_signed_download(self):
        for name in VIDEO_CASES:
            case = FIXTURE[name]
            project = case['request'][1][5]
            media_id = case['detail_request'][0]
            for stage, suffix in (('submit', 'PENDING'), ('active', 'ACTIVE'), ('complete', 'SUCCESSFUL'), ('detail', 'SUCCESSFUL')):
                rpc = case['rpc_id'] if stage == 'submit' else 'as29s' if stage == 'detail' else 'jwpduf'
                payload = parse_rpc_response(rpc_text(rpc, case[stage]), rpc)
                result = video_operations(payload, token_id=7, project_id=project, expected_ids=[media_id])['operations'][0]
                with self.subTest(name=name, stage=stage):
                    self.assertEqual(result['status'], 'MEDIA_GENERATION_STATUS_' + suffix)
                    url = result['operation'].get('metadata', {}).get('video', {}).get('fifeUrl')
                    self.assertEqual(bool(url), stage == 'detail')
                    if url:
                        self.assertIn('/video/' + media_id + '?', url)

    def test_g9_quota_is_not_traffic_or_login_failure(self):
        frame = FIXTURE['quota']['response_frame']
        with self.assertRaises(AngularRpcRejected) as caught:
            parse_rpc_response(json.dumps([frame]), 'MZZa6b')
        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(caught.exception.public_error, 'PUBLIC_ERROR_USER_QUOTA_REACHED')
        self.assertTrue(is_quota_exhausted_error(caught.exception))

    def test_image_models_still_match_current_capture(self):
        for name in ('image_pro', 'image_flash'):
            case = FIXTURE[name]
            payload = case['request']
            entry, ctx = payload[1][0], payload[3]
            rest = {'requests': [{'imageModelName': entry[5], 'seed': entry[3],
                    'imageAspectRatio': 'IMAGE_ASPECT_RATIO_LANDSCAPE' if entry[4] == 3 else 'IMAGE_ASPECT_RATIO_SQUARE',
                    'structuredPrompt': {'parts': [{'text': entry[8][0][0][0]}]},
                    'imageInputs': [{'name': ref[0], 'imageInputType': 'IMAGE_INPUT_TYPE_REFERENCE'} for ref in entry[2]]}],
                    'clientContext': {'projectId': ctx[5], 'recaptchaContext': {'token': ctx[10][0]}},
                    'mediaGenerationContext': {'batchId': payload[4][0]}}
            with self.subTest(name=name), patch('src.services.flow_angular.uuid.uuid4', side_effect=entry[12:14]):
                self.assertEqual(build_image_rpc(rest), ('ogiZ0b', payload))
                result = image_result(case['result'], ctx[5])
                self.assertEqual(len(result['media']), 1)


class CapturedVideoRoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = FlowClient(None, SimpleNamespace(get_token=AsyncMock(return_value=SimpleNamespace(auth_mode='flow'))))
        self.client._set_request_fingerprint({'native_token_id': 7})
        self.addCleanup(self.client.clear_request_fingerprint)
        self.client._make_request = AsyncMock(side_effect=AssertionError('No legacy fallback'))
        self.client._get_recaptcha_token = AsyncMock(return_value=('test-captcha', 'native:7'))
        self.client._acquire_video_launch_gate = AsyncMock(return_value=(True, 0, 0))
        self.client._release_video_launch_gate = AsyncMock()
        self.client._notify_browser_captcha_request_finished = AsyncMock()
        self.service = SimpleNamespace(fetch_json=AsyncMock())
        for patcher in (
            patch('src.services.flow_client.config', SimpleNamespace(captcha_method='native_cdp', flow_angular_video_models=[], flow_angular_video_families=[])),
            patch('src.services.browser_captcha_native_cdp.BrowserCaptchaService.get_instance', AsyncMock(return_value=self.service)),
            patch.object(self.client, '_captcha_aware_max_retries', return_value=3),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def generate(self, name):
        case = FIXTURE[name]
        rest = request(case)
        data = rest['requests'][0]
        args = dict(at=None, project_id=rest['clientContext']['projectId'], prompt='A bird takes flight.',
                    model_key=data['videoModelKey'], aspect_ratio=data['aspectRatio'], token_id=7,
                    output_resolution=data['outputSpec']['resolution'])
        if name == 'first_last_360p':
            return await self.client.generate_video_start_end(**args, use_v2_model_config=True,
                start_media_id=data['startImage']['mediaId'], end_media_id=data['endImage']['mediaId'],
                start_crop=data['startImage']['cropCoordinates'], end_crop=data['endImage']['cropCoordinates'])
        return await self.client.generate_video_reference_images(**args, reference_images=data['referenceImages'])

    async def test_submit_poll_resolve_uses_same_account_and_project(self):
        for name in VIDEO_CASES:
            case = FIXTURE[name]
            self.service.fetch_json.reset_mock()
            self.service.fetch_json.side_effect = [{'rpc_payload': case[stage]} for stage in ('submit', 'active', 'complete', 'detail')]
            launched = await self.generate(name)
            active = await self.client.check_video_status(None, launched['operations'])
            done = await self.client.check_video_status(None, active['operations'])
            self.assertIn('fifeUrl', done['operations'][0]['operation']['metadata']['video'])
            calls = self.service.fetch_json.await_args_list
            self.assertEqual([c.kwargs['json_data']['rpc_id'] for c in calls], [case['rpc_id'], 'jwpduf', 'jwpduf', 'as29s'])
            self.assertEqual(calls[1].kwargs['json_data']['payload'], case['poll_request'])
            self.assertEqual(calls[3].kwargs['json_data']['payload'], case['detail_request'])
            for call in calls:
                self.assertEqual(call.kwargs['url'], FLOW_RPC_URL)
                self.assertEqual(call.kwargs['token_id'], 7)
                self.assertEqual(call.kwargs['project_id'], case['request'][1][5])
                self.assertNotIn('headers', call.kwargs)
            self.assertTrue(calls[0].kwargs['consume_video_reservation'])
        self.client._make_request.assert_not_awaited()

    async def test_new_first_last_rpc_is_mutating_and_never_replayed_on_uncertainty(self):
        self.assertIn('nprQif', VIDEO_GENERATION_RPC_IDS)
        self.assertIn('nprQif', MUTATING_RPC_IDS)
        self.service.fetch_json.return_value = {'rpc_payload': []}
        with self.assertRaises(AngularSubmissionUncertain):
            await self.generate('first_last_360p')
        self.service.fetch_json.assert_awaited_once()
        self.client._notify_browser_captcha_request_finished.assert_awaited_once_with('native:7')
        self.client._make_request.assert_not_awaited()
        browser = NativeCdpAccountBrowser(7, None)
        browser._prepare_profile = AsyncMock()
        browser._get_or_create_project_session = AsyncMock(return_value=('target', 'session'))
        browser._evaluate = AsyncMock(side_effect=asyncio.TimeoutError())
        with self.assertRaises(AngularSubmissionUncertain):
            await browser.fetch_json(project_id='project-test', url=FLOW_RPC_URL,
                json_data={'rpc_id': 'nprQif', 'payload': FIXTURE['first_last_360p']['request']})
        browser._evaluate.assert_awaited_once()
        self.assertEqual(browser.busy_count, 0)
