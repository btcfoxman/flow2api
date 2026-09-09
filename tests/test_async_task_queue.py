import asyncio
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.api import routes
from src.core.database import Database
from src.core.models import Task, Token


class AsyncTaskQueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(db_path=f"{self._temp_dir.name}/flow.db")
        await self.db.init_db()

    async def asyncTearDown(self):
        self._temp_dir.cleanup()

    async def _enqueue(self, task_id: str, capacity: int = 50):
        return await self.db.enqueue_async_task(
            task_id=task_id,
            task_type="video",
            model="abra_t2v_4s_360p",
            prompt=task_id,
            request_payload="{}",
            base_url_override=None,
            capacity=capacity,
        )

    async def test_capacity_is_atomic_under_concurrent_enqueue(self):
        results = await asyncio.gather(
            *(self._enqueue(f"task-{index}", capacity=5) for index in range(20))
        )

        self.assertEqual(sum(result is not None for result in results), 5)
        stats = await self.db.get_async_task_queue_stats()
        self.assertEqual(stats["active"], 5)

    async def test_claim_preserves_fifo_and_blocks_overtaking(self):
        await self._enqueue("first")
        await self._enqueue("second")

        first = await self.db.claim_next_async_task()
        blocked = await self.db.claim_next_async_task()

        self.assertEqual(first["task_id"], "first")
        self.assertIsNone(blocked)
        self.assertEqual(await self.db.get_async_task_position("second"), 2)

        await self.db.delete_async_task("first")
        second = await self.db.claim_next_async_task()
        self.assertEqual(second["task_id"], "second")

    async def test_parallel_claim_window_preserves_fifo_order(self):
        await self._enqueue("first")
        await self._enqueue("second")
        await self._enqueue("third")

        first = await self.db.claim_next_async_task(max_submitting=2)
        second = await self.db.claim_next_async_task(max_submitting=2)
        blocked = await self.db.claim_next_async_task(max_submitting=2)

        self.assertEqual(first["task_id"], "first")
        self.assertEqual(second["task_id"], "second")
        self.assertIsNone(blocked)

        await self.db.delete_async_task("first")
        third = await self.db.claim_next_async_task(max_submitting=2)
        self.assertEqual(third["task_id"], "third")

    async def test_retry_backoff_keeps_queue_head_from_being_overtaken(self):
        await self._enqueue("first")
        await self._enqueue("second")
        first = await self.db.claim_next_async_task(max_submitting=2)
        await self.db.update_async_task(
            first["task_id"],
            status="queued",
            retry_after_seconds=60,
        )

        blocked = await self.db.claim_next_async_task(max_submitting=2)

        self.assertIsNone(blocked)
        self.assertEqual(await self.db.get_async_task_position("first"), 1)
        self.assertEqual(await self.db.get_async_task_position("second"), 2)

    async def test_interrupted_submission_is_recovered(self):
        await self._enqueue("recover-me")
        claimed = await self.db.claim_next_async_task()
        self.assertEqual(claimed["status"], "submitting")

        recovered = await self.db.reset_submitting_async_tasks()
        claimed_again = await self.db.claim_next_async_task()

        self.assertEqual(recovered, 1)
        self.assertEqual(claimed_again["task_id"], "recover-me")

    async def test_no_available_account_returns_item_to_queue_head(self):
        normalized = routes.NormalizedGenerationRequest(
            model="abra_t2v_4s_360p",
            prompt="wait for an account",
            images=[],
        )
        await self.db.enqueue_async_task(
            task_id="waiting-task",
            task_type="video",
            model=normalized.model,
            prompt=normalized.prompt,
            request_payload=routes._serialize_normalized_generation_request(normalized),
            base_url_override=None,
            capacity=50,
        )
        claimed = await self.db.claim_next_async_task()
        handler = SimpleNamespace(
            db=self.db,
            load_balancer=SimpleNamespace(
                select_token=AsyncMock(return_value=None),
                get_unavailable_reason=AsyncMock(return_value="proxy cooling down"),
            ),
        )

        with patch.object(routes, "generation_handler", handler):
            retry_delay = await routes._process_async_video_queue_item(claimed)

        queued = await self.db.get_async_task("waiting-task")
        self.assertEqual(retry_delay, routes.ASYNC_TASK_QUEUE_RETRY_SECONDS)
        self.assertEqual(queued["status"], "queued")
        self.assertEqual(queued["last_error"], "proxy cooling down")

    async def test_upload_timeout_retains_payload_and_queue_position(self):
        from src.core.media_errors import project_image_upload_failure_response
        normalized = routes.NormalizedGenerationRequest(
            model="abra_r2v_4s_360p", prompt="upload timeout", images=[b"reference"],
        )
        payload = routes._serialize_normalized_generation_request(normalized)
        await self.db.enqueue_async_task(
            task_id="upload-timeout", task_type="video", model=normalized.model,
            prompt=normalized.prompt, request_payload=payload,
            base_url_override=None, capacity=50,
        )
        claimed = await self.db.claim_next_async_task()
        message, status = project_image_upload_failure_response(
            "Project-scoped image upload failed via /flow/uploadImage (cause=TimeoutError)"
        )
        handler = SimpleNamespace(db=self.db, load_balancer=SimpleNamespace(
            select_token=AsyncMock(return_value=SimpleNamespace(id=43)),
        ))
        with (patch.object(routes, "generation_handler", handler),
              patch.object(routes, "_collect_async_video_task_result", new=AsyncMock(
                  return_value={"error": {"message": message, "status_code": status}},
              ))):
            delay = await routes._process_async_video_queue_item(claimed)
        queued = await self.db.get_async_task("upload-timeout")
        self.assertGreater(delay, 0)
        self.assertEqual(queued["status"], "queued")
        self.assertEqual(queued["request_payload"], payload)
        self.assertEqual(queued["attempt_count"], 1)
        self.assertFalse(queued["upstream_task_id"])
        self.assertEqual(await self.db.get_async_task_position("upload-timeout"), 1)

    async def test_successful_submission_moves_item_to_normal_task_table(self):
        token_id = await self.db.add_token(
            Token(st="st-queue", at="at-queue", email="queue@example.com")
        )
        normalized = routes.NormalizedGenerationRequest(
            model="abra_t2v_4s_360p",
            prompt="submit in order",
            images=[],
        )
        await self.db.enqueue_async_task(
            task_id="public-task",
            task_type="video",
            model=normalized.model,
            prompt=normalized.prompt,
            request_payload=routes._serialize_normalized_generation_request(normalized),
            base_url_override="https://example.test",
            capacity=50,
        )
        claimed = await self.db.claim_next_async_task()
        await self.db.create_task(
            Task(
                task_id="upstream-task",
                token_id=token_id,
                model="abra_t2v_4s_360p",
                prompt=normalized.prompt,
                status="completed",
                progress=100,
                operations=[{"operation": {"name": "upstream-task"}}],
            )
        )
        handler = SimpleNamespace(
            db=self.db,
            load_balancer=SimpleNamespace(
                select_token=AsyncMock(return_value=SimpleNamespace(id=token_id)),
            ),
        )

        with (
            patch.object(routes, "generation_handler", handler),
            patch.object(
                routes,
                "_collect_async_video_task_result",
                new=AsyncMock(return_value={"id": "upstream-task"}),
            ),
        ):
            retry_delay = await routes._process_async_video_queue_item(claimed)

        local_task = await self.db.get_task("public-task")
        self.assertEqual(retry_delay, 0)
        self.assertIsNone(await self.db.get_async_task("public-task"))
        self.assertIsNotNone(local_task)
        self.assertEqual(local_task.status, "completed")
        self.assertEqual(local_task.token_id, token_id)


if __name__ == "__main__":
    unittest.main()
