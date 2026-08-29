import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from src.services.generation_handler import GenerationHandler


TRAFFIC_ERROR = (
    "Flow native browser API request failed: "
    "PUBLIC_ERROR_UNUSUAL_ACTIVITY_TOO_MUCH_TRAFFIC: reCAPTCHA evaluation failed"
)


class TrafficAccountSwitchingTests(unittest.IsolatedAsyncioTestCase):
    def _make_handler(self, *, failing_token_ids, token_ids=(1, 2, 3)):
        handler = GenerationHandler.__new__(GenerationHandler)
        handler._background_tasks = set()
        handler.flow_client = SimpleNamespace(
            clear_request_fingerprint=MagicMock(),
            prefill_remote_browser_pool=AsyncMock(),
            _activate_traffic_cooldown=MagicMock(return_value=120),
        )
        tokens = [
            SimpleNamespace(
                id=token_id,
                email=f"user{token_id}@example.com",
                user_paygate_tier="PAYGATE_TIER_ONE",
            )
            for token_id in token_ids
        ]
        exclusions = []

        async def select_token(**kwargs):
            excluded = set(kwargs.get("exclude_token_ids") or ())
            exclusions.append(excluded)
            return next((token for token in tokens if token.id not in excluded), None)

        handler.load_balancer = SimpleNamespace(
            select_token=AsyncMock(side_effect=select_token),
            release_pending=AsyncMock(),
            get_unavailable_reason=AsyncMock(return_value=None),
        )
        handler.token_manager = SimpleNamespace(
            ensure_valid_token=AsyncMock(side_effect=lambda token: token),
            ensure_project_exists=AsyncMock(
                side_effect=lambda token_id: f"project-{token_id}"
            ),
            record_usage=AsyncMock(),
            record_success=AsyncMock(),
            record_error=AsyncMock(),
        )
        handler._update_request_log_progress = AsyncMock()
        handler._log_request = AsyncMock(return_value=1)
        attempted = []

        async def generate_video(token, *args, **kwargs):
            attempted.append(token.id)
            if token.id in failing_token_ids:
                raise RuntimeError(TRAFFIC_ERROR)
            handler._mark_generation_succeeded(kwargs["generation_result"])
            yield handler._create_completion_response(
                "https://example.com/video.mp4",
                media_type="video",
            )

        handler._handle_video_generation = generate_video
        return handler, attempted, exclusions

    async def test_does_not_switch_accounts_for_shared_traffic_control(self):
        handler, attempted, exclusions = self._make_handler(failing_token_ids={1})

        chunks = [
            chunk
            async for chunk in handler.handle_generation(
                model="abra_t2v_10s",
                prompt="hello",
                stream=False,
            )
        ]

        self.assertEqual(attempted, [1])
        self.assertEqual(exclusions, [set()])
        self.assertEqual(handler.load_balancer.release_pending.await_count, 1)
        handler.flow_client._activate_traffic_cooldown.assert_called_once_with()
        final_log = handler._log_request.await_args
        self.assertEqual(final_log.kwargs["status_text"], "failed")
        self.assertNotIn(
            "traffic_account_switches",
            final_log.args[3]["performance"],
        )
        self.assertEqual(json.loads(chunks[-1])["error"]["status_code"], 429)

    async def test_repeated_traffic_control_still_submits_only_once(self):
        handler, attempted, exclusions = self._make_handler(
            failing_token_ids={1, 2, 3}
        )

        chunks = [
            chunk
            async for chunk in handler.handle_generation(
                model="abra_t2v_10s",
                prompt="hello",
                stream=False,
            )
        ]

        self.assertEqual(attempted, [1])
        self.assertEqual(exclusions, [set()])
        payload = json.loads(chunks[-1])
        self.assertEqual(payload["error"]["status_code"], 429)

    async def test_external_v2v_media_id_does_not_switch_accounts(self):
        handler, attempted, exclusions = self._make_handler(failing_token_ids={1})

        chunks = [
            chunk
            async for chunk in handler.handle_generation(
                model="abra_edit",
                prompt="hello",
                images=[b"image"],
                video_media_id="account-scoped-video-id",
                stream=False,
            )
        ]

        self.assertEqual(attempted, [1])
        self.assertEqual(exclusions, [set()])
        self.assertEqual(json.loads(chunks[-1])["error"]["status_code"], 429)

    async def test_uploaded_v2v_bytes_also_does_not_switch_accounts(self):
        handler, attempted, _ = self._make_handler(failing_token_ids={1})

        chunks = [
            chunk
            async for chunk in handler.handle_generation(
                model="abra_edit",
                prompt="hello",
                images=[b"image"],
                video_bytes=b"video",
                stream=False,
            )
        ]

        self.assertEqual(attempted, [1])
        self.assertEqual(json.loads(chunks[-1])["error"]["status_code"], 429)


if __name__ == "__main__":
    unittest.main()
