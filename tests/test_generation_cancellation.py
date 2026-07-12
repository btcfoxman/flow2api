import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from src.services.generation_handler import GenerationHandler


class GenerationCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_image_request_is_finalized_instead_of_left_processing(self):
        handler = GenerationHandler.__new__(GenerationHandler)
        handler._background_tasks = set()
        handler.flow_client = SimpleNamespace(
            clear_request_fingerprint=MagicMock(),
            prefill_remote_browser_pool=AsyncMock(),
        )
        token = SimpleNamespace(
            id=8,
            email="image@example.com",
            user_paygate_tier="PAYGATE_TIER_ONE",
        )
        handler.load_balancer = SimpleNamespace(
            select_token=AsyncMock(return_value=token),
            release_pending=AsyncMock(),
        )
        handler.token_manager = SimpleNamespace(
            ensure_valid_token=AsyncMock(return_value=token),
            ensure_project_exists=AsyncMock(return_value="project-1"),
        )
        handler._update_request_log_progress = AsyncMock()
        handler._log_request = AsyncMock(return_value=91)

        async def cancelled_image(*args, **kwargs):
            raise asyncio.CancelledError()
            yield  # pragma: no cover

        handler._handle_image_generation = cancelled_image

        with self.assertRaises(asyncio.CancelledError):
            async for _ in handler.handle_generation(
                model="gemini-3.0-pro-image-landscape",
                prompt="hello",
                stream=False,
            ):
                pass

        final_call = handler._log_request.call_args
        self.assertEqual(final_call.args[4], 499)
        self.assertEqual(final_call.kwargs["status_text"], "failed")
        self.assertEqual(final_call.kwargs["progress"], 100)
        handler.load_balancer.release_pending.assert_awaited_once()

    async def test_submitted_sync_video_continues_after_request_cancellation(self):
        handler = GenerationHandler.__new__(GenerationHandler)
        handler.db = SimpleNamespace(create_task=AsyncMock())
        handler.flow_client = SimpleNamespace(
            generate_video_text=AsyncMock(
                return_value={
                    "operations": [
                        {
                            "operation": {"name": "upstream-task-1"},
                            "sceneId": "scene-1",
                        }
                    ]
                }
            )
        )
        handler._update_request_log_progress = AsyncMock()
        handler._resolve_video_model_key_for_tier = MagicMock(
            return_value=("abra_t2v_10s", None)
        )

        async def cancelled_poll(*args, **kwargs):
            raise asyncio.CancelledError()
            yield  # pragma: no cover

        handler._poll_video_result = cancelled_poll
        handler._run_video_task_background = AsyncMock()
        spawned = []

        def capture_background(coro):
            spawned.append(coro)
            coro.close()
            return MagicMock()

        handler._spawn_background_task = capture_background

        token = SimpleNamespace(
            id=7,
            at="at-token",
            st="st-token",
            user_paygate_tier="PAYGATE_TIER_ONE",
            video_concurrency=1,
        )
        model_config = {
            "video_type": "t2v",
            "supports_images": False,
            "min_images": 0,
            "max_images": 0,
            "model_key": "abra_t2v_10s",
            "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        }
        response_state = handler._create_response_state()
        request_log_state = {
            "id": 42,
            "progress": 45,
            "started_at": 1.0,
            "operation": "generate_video",
            "request_payload": {"model": "veo"},
        }

        with self.assertRaises(asyncio.CancelledError):
            async for _ in handler._handle_video_generation(
                token=token,
                project_id="project-1",
                model_config=model_config,
                prompt="hello",
                images=None,
                stream=False,
                generation_result=handler._create_generation_result(),
                response_state=response_state,
                request_log_state=request_log_state,
                async_task=False,
            ):
                pass

        self.assertEqual(response_state["submitted_video_task_id"], "upstream-task-1")
        self.assertTrue(response_state["video_continuation_started"])
        self.assertEqual(len(spawned), 1)
        handler._run_video_task_background.assert_called_once()
        call_kwargs = handler._run_video_task_background.call_args.kwargs
        self.assertTrue(call_kwargs["request_log_state"]["async_result_log"])
        self.assertTrue(call_kwargs["record_usage_on_success"])


if __name__ == "__main__":
    unittest.main()
