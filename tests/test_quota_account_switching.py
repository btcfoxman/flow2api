import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from src.services.generation_handler import GenerationHandler


QUOTA_ERROR = (
    "Flow API request failed: PUBLIC_ERROR_USER_QUOTA_REACHED: "
    "Resource has been exhausted (e.g. check quota)."
)


class QuotaAccountSwitchingTests(unittest.IsolatedAsyncioTestCase):
    def _make_handler(self, *, quota_token_ids, submitted_token_ids=()):
        handler = GenerationHandler.__new__(GenerationHandler)
        handler._background_tasks = set()
        handler.flow_client = SimpleNamespace(
            clear_request_fingerprint=MagicMock(),
            prefill_remote_browser_pool=AsyncMock(),
        )
        tokens = [
            SimpleNamespace(
                id=token_id,
                email=f"user{token_id}@example.com",
                user_paygate_tier="PAYGATE_TIER_ONE",
            )
            for token_id in range(1, 5)
        ]
        selection_exclusions = []

        async def select_token(**kwargs):
            excluded = set(kwargs.get("exclude_token_ids") or ())
            selection_exclusions.append(excluded)
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
            mark_quota_exhausted=AsyncMock(return_value=0),
            record_usage=AsyncMock(),
            record_success=AsyncMock(),
            record_error=AsyncMock(),
        )
        handler._update_request_log_progress = AsyncMock()
        handler._log_request = AsyncMock(return_value=1)
        attempted_token_ids = []

        async def generate_video(
            token,
            project_id,
            model_config,
            prompt,
            images,
            stream,
            **kwargs,
        ):
            attempted_token_ids.append(token.id)
            if token.id in submitted_token_ids:
                kwargs["response_state"]["submitted_video_task_id"] = f"task-{token.id}"
            if token.id in quota_token_ids:
                raise RuntimeError(QUOTA_ERROR)
            handler._mark_generation_succeeded(kwargs["generation_result"])
            yield handler._create_completion_response(
                "https://example.com/video.mp4",
                media_type="video",
            )

        handler._handle_video_generation = generate_video
        return handler, attempted_token_ids, selection_exclusions

    async def _collect(self, handler):
        return [
            chunk
            async for chunk in handler.handle_generation(
                model="abra_t2v_10s",
                prompt="hello",
                stream=False,
            )
        ]

    async def test_switches_twice_then_submits_with_third_account(self):
        handler, attempted, exclusions = self._make_handler(
            quota_token_ids={1, 2},
        )

        chunks = await self._collect(handler)

        self.assertEqual(attempted, [1, 2, 3])
        self.assertEqual(exclusions, [set(), {1}, {1, 2}])
        self.assertEqual(handler.token_manager.mark_quota_exhausted.await_count, 2)
        self.assertEqual(handler.load_balancer.release_pending.await_count, 3)
        payload = json.loads(chunks[-1])
        self.assertEqual(payload["choices"][0]["finish_reason"], "stop")

    async def test_stops_after_two_switches_and_never_uses_fourth_account(self):
        handler, attempted, exclusions = self._make_handler(
            quota_token_ids={1, 2, 3, 4},
        )

        chunks = await self._collect(handler)

        self.assertEqual(attempted, [1, 2, 3])
        self.assertEqual(exclusions, [set(), {1}, {1, 2}])
        self.assertEqual(handler.token_manager.mark_quota_exhausted.await_count, 3)
        payload = json.loads(chunks[-1])
        message = payload["error"]["message"]
        self.assertEqual(payload["error"]["status_code"], 503)
        self.assertIn("账号额度不足", message)
        self.assertNotIn("flow", message.lower())
        self.assertNotIn("上游", message)

    async def test_does_not_resubmit_after_remote_task_id_exists(self):
        handler, attempted, exclusions = self._make_handler(
            quota_token_ids={1},
            submitted_token_ids={1},
        )

        chunks = await self._collect(handler)

        self.assertEqual(attempted, [1])
        self.assertEqual(exclusions, [set()])
        payload = json.loads(chunks[-1])
        self.assertEqual(payload["error"]["status_code"], 503)


if __name__ == "__main__":
    unittest.main()
