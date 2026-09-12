import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from src.services.generation_handler import GenerationHandler


class AsyncVideoSuccessAccountingTests(unittest.IsolatedAsyncioTestCase):
    async def test_accepted_async_submission_is_not_counted_as_completed(self):
        handler = GenerationHandler.__new__(GenerationHandler)
        handler.db = SimpleNamespace(get_active_tokens=AsyncMock(return_value=[]))
        handler._background_tasks = set()
        handler.flow_client = SimpleNamespace(
            clear_request_fingerprint=MagicMock(),
            prefill_remote_browser_pool=AsyncMock(),
        )
        token = SimpleNamespace(
            id=7,
            email="video@example.com",
            user_paygate_tier="PAYGATE_TIER_ONE",
        )
        handler.load_balancer = SimpleNamespace(
            select_token=AsyncMock(return_value=token),
            release_pending=AsyncMock(),
            get_unavailable_reason=AsyncMock(return_value=None),
        )
        handler.token_manager = SimpleNamespace(
            ensure_valid_token=AsyncMock(return_value=token),
            ensure_project_exists=AsyncMock(return_value="project-1"),
            record_usage=AsyncMock(),
            record_success=AsyncMock(),
            record_error=AsyncMock(),
        )
        handler._update_request_log_progress = AsyncMock()
        handler._log_request = AsyncMock(return_value=1)

        async def accept_async_video(*args, **kwargs):
            kwargs["response_state"]["async_task_id"] = "task-accepted"
            handler._mark_generation_succeeded(kwargs["generation_result"])
            yield handler._create_video_task_response(
                task_id="task-accepted",
                model="abra_t2v_10s",
                status="processing",
                progress=45,
            )

        handler._handle_video_generation = accept_async_video

        with patch(
            "src.services.generation_handler.record_generation_result"
        ) as record_result:
            async for _ in handler.handle_generation(
                model="abra_t2v_10s",
                prompt="hello",
                stream=False,
                async_video_task=True,
            ):
                pass

        handler.token_manager.record_usage.assert_not_awaited()
        handler.token_manager.record_success.assert_not_awaited()
        record_result.assert_not_called()
        self.assertEqual(
            handler._log_request.await_args.kwargs["status_text"],
            "video_submitted",
        )

    async def test_async_worker_records_usage_after_final_success(self):
        handler = GenerationHandler.__new__(GenerationHandler)
        handler.token_manager = SimpleNamespace(
            record_usage=AsyncMock(),
            record_success=AsyncMock(),
            record_error=AsyncMock(),
        )
        handler.load_balancer = None

        async def complete_video(*args, **kwargs):
            generation_result = args[5]
            handler._mark_generation_succeeded(generation_result)
            if False:
                yield None

        handler._poll_video_result = complete_video
        token = SimpleNamespace(id=9)

        with patch(
            "src.services.generation_handler.record_generation_result"
        ) as record_result:
            await handler._run_video_task_background(
                token=token,
                project_id="project-1",
                operations=[{"operation": {"name": "task-completed"}}],
                upsample_config=None,
                response_state={},
                request_log_state=None,
                extend_source_media_id=None,
                watermark=False,
                record_usage_on_success=True,
            )

        handler.token_manager.record_usage.assert_awaited_once_with(
            token.id,
            is_video=True,
        )
        handler.token_manager.record_success.assert_awaited_once_with(token.id)
        self.assertEqual(record_result.call_args.args[:2], ("video", "success"))

    async def test_async_submit_enables_completion_accounting_in_worker(self):
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
        handler._start_async_video_result_log = AsyncMock(
            return_value={"id": 12, "async_result_log": True}
        )
        handler._resolve_video_model_key_for_tier = MagicMock(
            return_value=("abra_t2v_10s", None)
        )
        handler._run_video_task_background = AsyncMock()

        def capture_background(coro):
            coro.close()
            return MagicMock()

        handler._spawn_background_task = capture_background
        token = SimpleNamespace(
            id=7,
            at="at-token",
            st="st-token",
            user_paygate_tier="PAYGATE_TIER_ONE",
            video_concurrency=-1,
        )
        model_config = {
            "video_type": "t2v",
            "supports_images": False,
            "min_images": 0,
            "max_images": 0,
            "model_key": "abra_t2v_10s",
            "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        }

        async for _ in handler._handle_video_generation(
            token=token,
            project_id="project-1",
            model_config=model_config,
            prompt="hello",
            images=None,
            stream=False,
            generation_result=handler._create_generation_result(),
            response_state=handler._create_response_state(),
            request_log_state={"id": 4, "progress": 0},
            async_task=True,
        ):
            pass

        call_kwargs = handler._run_video_task_background.call_args.kwargs
        self.assertTrue(call_kwargs["record_usage_on_success"])


if __name__ == "__main__":
    unittest.main()
