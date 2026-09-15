import asyncio
import json
import tempfile
import unittest
from unittest.mock import AsyncMock

from src.core.async_queue import queue_submission_guard, queued_task_id
from src.core.database import Database
from src.core.models import RequestLog, Task, Token
from src.services.generation_handler import GenerationHandler


class AsyncOutcomeAccountingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(db_path=f"{self.temp.name}/flow.db")
        await self.db.init_db()
        self.token_id = await self.db.add_token(Token(st="test", email="test@example.test"))
        self.handler = GenerationHandler.__new__(GenerationHandler)
        self.handler.db = self.db

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def enqueue(self, task_id="public-task", capacity=50):
        return await self.db.enqueue_async_task(task_id=task_id, task_type="video",
            model="abra_r2v_4s_360p", prompt="test", request_payload="{}",
            base_url_override=None, capacity=capacity, timeout_seconds=60)

    async def log(self, status, code, operation="generate_video"):
        return await self.handler._log_request(self.token_id, operation, {}, {}, code, 1,
            status_text=status, progress=100)

    async def task(self, task_id="public-task", status="completed"):
        await self.db.create_task(Task(task_id=task_id, token_id=self.token_id,
            model="abra_r2v_4s_360p", prompt="test", status=status, progress=100))

    async def test_retry_then_success_counts_one_public_job_and_survives_restart_cleanup(self):
        await self.enqueue()
        with queue_submission_guard(AsyncMock(return_value=True), task_id="public-task"):
            retry_id = await self.log("failed", 503)
            await self.log("failed", 429)
            await self.log("completed", 200, "generate_video_async_result")
        self.assertEqual((await self.db.get_generation_outcome_stats())["total_successes"], 0)
        async with self.db._connect() as conn:
            row = await (await conn.execute("SELECT status_text,request_body FROM request_logs WHERE id=?", (retry_id,))).fetchone()
        self.assertEqual(row[0], "retrying")
        self.assertEqual(json.loads(row[1])["queue_task_id"], "public-task")
        await self.task("upstream-id")
        await self.task()
        await self.db.delete_async_task("public-task")
        await self.db.update_task("public-task", status="completed")
        await self.db.init_db()
        stats = await self.db.get_generation_outcome_stats()
        self.assertEqual(stats["total_successes"], 1)
        self.assertEqual(stats["total_videos"], 1)
        self.assertEqual(stats["total_failed_tasks"], 0)
        await self.db.clear_all_logs()
        await self.db.init_db()
        self.assertEqual(await self.db.get_generation_outcome_stats(), stats)

    async def test_uncertain_submit_and_poll_failure_each_count_once(self):
        await self.enqueue("uncertain")
        with queue_submission_guard(AsyncMock(), task_id="uncertain"):
            await self.log("failed", 500)
        await self.db.update_async_task("uncertain", status="failed")
        await self.enqueue("poll")
        await self.task("poll", "processing")
        with queue_submission_guard(AsyncMock(), task_id="poll"):
            await self.log("failed", 502, "generate_video_async_result")
        await self.db.update_task("poll", status="failed")
        await self.db.delete_async_task("poll")
        await self.db.init_db()
        self.assertEqual((await self.db.get_generation_outcome_stats())["total_failed_tasks"], 2)

    async def test_expiry_and_crash_uncertainty_count_without_attempt_logs(self):
        await self.enqueue("expired")
        async with self.db._connect(write=True) as conn:
            await conn.execute("UPDATE async_task_queue SET expires_at=1 WHERE task_id='expired'")
            await conn.commit()
        await self.db.expire_async_tasks()
        await self.enqueue("interrupted")
        await self.db.claim_next_async_task()
        self.assertTrue(await self.db.admit_async_task_submission("interrupted"))
        await self.db.reset_submitting_async_tasks()
        self.assertEqual((await self.db.get_generation_outcome_stats())["total_failed_tasks"], 2)

    async def test_rejected_capacity_and_unaccepted_logs_are_not_queue_jobs(self):
        await self.enqueue("accepted", 1)
        self.assertIsNone(await self.enqueue("rejected", 1))
        async with self.db._connect() as conn:
            self.assertEqual((await (await conn.execute("SELECT COUNT(*) FROM async_generation_outcomes")).fetchone())[0], 1)
        await self.db.add_request_log(RequestLog(operation="generate_image", status_text="completed",
            status_code=200, progress=100, duration=1, request_body="not json"))
        await self.db.init_db()
        stats = await self.db.get_generation_outcome_stats()
        self.assertEqual(stats["total_images"], 1)
        self.assertEqual(stats["total_failed_tasks"], 0)

    async def test_context_is_task_local_and_copied_to_background_poll(self):
        async def read_later():
            await asyncio.sleep(0)
            return queued_task_id()
        with queue_submission_guard(AsyncMock(), task_id="outer"):
            background = asyncio.create_task(read_later())
            with queue_submission_guard(AsyncMock(), task_id="inner"):
                self.assertEqual(queued_task_id(), "inner")
            self.assertEqual(queued_task_id(), "outer")
        self.assertIsNone(queued_task_id())
        self.assertEqual(await background, "outer")
