import asyncio
import time
import tempfile
import unittest
import httpx
from fastapi import FastAPI
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.api import routes
from src.core.async_queue import (
    AsyncQueueExpired, QUEUE_TIMEOUT_MESSAGE, QUEUE_SUBMISSION_UNCERTAIN_MESSAGE,
    admit_queued_video_submission, normalize_queue_timeout, queue_submission_guard,
)
from src.core.database import Database
from src.services.flow_client import FlowClient


class AsyncTaskQueueExpiryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(db_path=f"{self.temp_dir.name}/flow.db")
        await self.db.init_db()
        self.normalized = routes.NormalizedGenerationRequest(
            model="abra_r2v_4s_360p", prompt="queue expiry regression", images=[b"image"],
        )

    async def asyncTearDown(self):
        self.temp_dir.cleanup()

    async def enqueue(self, task_id="task", capacity=50, timeout_seconds=1500):
        return await self.db.enqueue_async_task(
            task_id=task_id, task_type="video", model=self.normalized.model,
            prompt=self.normalized.prompt,
            request_payload=routes._serialize_normalized_generation_request(self.normalized),
            base_url_override=None, capacity=capacity, timeout_seconds=timeout_seconds,
        )

    async def make_overdue(self, task_id="task"):
        async with self.db._connect(write=True) as connection:
            await connection.execute("UPDATE async_task_queue SET expires_at = ? WHERE task_id = ?", (time.time()-1, task_id))
            await connection.commit()

    def handler(self, **load_balancer):
        return SimpleNamespace(db=self.db, load_balancer=SimpleNamespace(**load_balancer))

    async def test_deadline_is_persisted_once_and_not_extended_by_retry(self):
        before = time.time()
        added = await self.enqueue(timeout_seconds=600)
        self.assertGreaterEqual(added["expires_at"], before + 600)
        await self.db.claim_next_async_task()
        await self.db.update_async_task("task", status="queued", retry_after_seconds=30, increment_attempt=True)
        retry = await self.db.get_async_task("task")
        self.assertEqual(retry["expires_at"], added["expires_at"])
        self.assertEqual(retry["attempt_count"], 1)

    async def test_sweep_expires_entire_pool_even_behind_a_live_backoff_head(self):
        await self.enqueue("head")
        await self.db.claim_next_async_task()
        await self.db.update_async_task("head", status="queued", retry_after_seconds=600)
        for task_id in ("second", "third"):
            await self.enqueue(task_id)
        for task_id in ("second", "third"):
            await self.make_overdue(task_id)
        self.assertEqual(await self.db.expire_async_tasks(), 2)
        self.assertIsNone(await self.db.claim_next_async_task())
        stats = await self.db.get_async_task_queue_stats()
        self.assertEqual((stats["active"], stats["failed"]), (1, 2))

    async def test_capacity_is_freed_atomically_when_expired_entries_are_present(self):
        await self.enqueue(capacity=1)
        await self.make_overdue()
        added = await self.enqueue("replacement", capacity=1)
        self.assertEqual(added["position"], 1)
        expired = await self.db.get_async_task("task")
        self.assertEqual(expired["status"], "failed")
        self.assertEqual(expired["request_payload"], "{}")

    async def test_expiry_is_committed_even_when_new_enqueue_is_still_rejected(self):
        await self.enqueue("expired", capacity=2)
        await self.enqueue("live", capacity=2)
        await self.make_overdue("expired")
        self.assertIsNone(await self.enqueue("new", capacity=1))
        self.assertEqual((await self.db.get_async_task("expired"))["status"], "failed")

    async def test_expired_terminal_task_cannot_be_revived_or_admitted(self):
        await self.enqueue()
        await self.db.claim_next_async_task()
        await self.make_overdue()
        self.assertEqual(await self.db.expire_async_tasks(), 1)
        changed = await self.db.update_async_task("task", status="queued", request_payload="resurrection", increment_attempt=True)
        self.assertFalse(changed)
        self.assertFalse(await self.db.admit_async_task_submission("task"))
        self.assertIsNone(await self.db.claim_next_async_task())
        row = await self.db.get_async_task("task")
        self.assertEqual(row["last_error"], QUEUE_TIMEOUT_MESSAGE)
        self.assertEqual(row["request_payload"], "{}")

    async def test_stale_claim_cannot_reach_account_selection_or_generation(self):
        await self.enqueue()
        claimed = await self.db.claim_next_async_task()
        await self.make_overdue()
        select = AsyncMock()
        with patch.object(routes, "generation_handler", self.handler(select_token=select)), \
             patch.object(routes, "_collect_async_video_task_result", AsyncMock()) as collect:
            self.assertEqual(await routes._process_async_video_queue_item(claimed), 0)
        select.assert_not_awaited()
        collect.assert_not_awaited()

    async def test_expiry_during_account_selection_prevents_generation(self):
        await self.enqueue()
        claimed = await self.db.claim_next_async_task()
        async def select(**kwargs):
            await self.make_overdue()
            return SimpleNamespace(id=1)
        with patch.object(routes, "generation_handler", self.handler(select_token=select)), \
             patch.object(routes, "_collect_async_video_task_result", AsyncMock()) as collect:
            self.assertEqual(await routes._process_async_video_queue_item(claimed), 0)
        collect.assert_not_awaited()

    async def test_expiry_during_generation_preparation_stops_final_submit(self):
        await self.enqueue()
        claimed = await self.db.claim_next_async_task()
        send = AsyncMock()
        async def collect(*args):
            await self.make_overdue()
            await admit_queued_video_submission()
            return await send()
        handler = self.handler(select_token=AsyncMock(return_value=SimpleNamespace(id=1)))
        with patch.object(routes, "generation_handler", handler), \
             patch.object(routes, "_collect_async_video_task_result", collect):
            self.assertEqual(await routes._process_async_video_queue_item(claimed), 0)
        send.assert_not_awaited()
        self.assertEqual((await self.db.get_async_task("task"))["status"], "failed")
        # Request-local context must not leak into unrelated requests.
        await admit_queued_video_submission()

    async def test_real_flow_client_admission_blocks_before_any_network(self):
        client = FlowClient.__new__(FlowClient)
        client._request_fingerprint_ctx = SimpleNamespace(get=lambda: None)
        client._make_request = AsyncMock()
        guard = AsyncMock(return_value=False)
        with queue_submission_guard(guard), self.assertRaises(AsyncQueueExpired):
            await client._make_video_api_request(
                url="https://aisandbox-pa.googleapis.com/v1/video:batchAsyncGenerateVideoText",
                json_data={}, at="test", timeout=30,
            )
        guard.assert_awaited_once()
        client._make_request.assert_not_awaited()

    async def test_expiration_error_does_not_trigger_upstream_retry_or_captcha_reset(self):
        client = FlowClient.__new__(FlowClient)
        client._notify_browser_captcha_error = AsyncMock()
        retry = await client._handle_retryable_generation_error(
            error=AsyncQueueExpired(), retry_attempt=0, max_retries=3,
            browser_id=1, project_id="project", log_prefix="test",
        )
        self.assertFalse(retry)
        client._notify_browser_captcha_error.assert_not_awaited()

    async def test_admitted_submission_is_not_swept_when_response_arrives_late(self):
        await self.enqueue()
        await self.db.claim_next_async_task()
        with queue_submission_guard(lambda: self.db.admit_async_task_submission("task")):
            await admit_queued_video_submission()
            await self.make_overdue()
            self.assertEqual(await self.db.expire_async_tasks(), 0)
            await self.db.update_async_task("task", upstream_task_id="upstream")
            # Existing-task follow-up work does not depend on a deleted queue row.
            await self.db.delete_async_task("task")
            await admit_queued_video_submission()

    async def test_expired_known_retry_ends_instead_of_reentering_queue(self):
        await self.enqueue()
        await self.db.claim_next_async_task()
        self.assertTrue(await self.db.admit_async_task_submission("task"))
        await self.make_overdue()
        await self.db.update_async_task("task", status="queued", last_error="HTTP 429", retry_after_seconds=30)
        row = await self.db.get_async_task("task")
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["last_error"], QUEUE_TIMEOUT_MESSAGE)
        self.assertEqual(row["next_attempt_at"], 0)

    async def test_admitted_payload_is_processing_not_still_waiting(self):
        await self.enqueue()
        await self.db.claim_next_async_task()
        await self.db.admit_async_task_submission("task")
        await self.make_overdue()
        with patch.object(routes, "generation_handler", self.handler()):
            payload = await routes._build_queued_video_task_payload(await self.db.get_async_task("task"))
        self.assertEqual(payload["status"], "processing")
        self.assertNotIn("error", payload)
        self.assertNotIn("queue_position", payload)

    async def test_restart_does_not_revive_expired_or_uncertain_submissions(self):
        await self.enqueue("expired")
        await self.db.claim_next_async_task()
        await self.make_overdue("expired")
        await self.enqueue("uncertain")
        await self.db.claim_next_async_task()
        self.assertTrue(await self.db.admit_async_task_submission("uncertain"))
        restarted = Database(db_path=self.db.db_path)
        await restarted.reset_submitting_async_tasks()
        self.assertEqual((await restarted.get_async_task("expired"))["last_error"], QUEUE_TIMEOUT_MESSAGE)
        self.assertEqual((await restarted.get_async_task("uncertain"))["last_error"], QUEUE_SUBMISSION_UNCERTAIN_MESSAGE)
        self.assertIsNone(await restarted.claim_next_async_task())

    async def test_restart_preserves_live_deadline_and_already_attached_upstream(self):
        added = await self.enqueue("waiting")
        await self.enqueue("attached")
        await self.db.claim_next_async_task(max_submitting=2)
        await self.db.claim_next_async_task(max_submitting=2)
        await self.db.update_async_task("attached", upstream_task_id="upstream")
        await self.make_overdue("attached")
        await self.db.reset_submitting_async_tasks()
        self.assertEqual((await self.db.get_async_task("waiting"))["expires_at"], added["expires_at"])
        attached = await self.db.get_async_task("attached")
        self.assertEqual(attached["status"], "queued")
        with patch.object(routes, "generation_handler", self.handler(select_token=AsyncMock())), \
             patch.object(routes, "_attach_queued_video_task", AsyncMock(return_value=True)) as attach, \
             patch.object(routes, "_collect_async_video_task_result", AsyncMock()) as submit:
            await routes._process_async_video_queue_item(attached)
        attach.assert_awaited_once()
        submit.assert_not_awaited()

    async def test_polling_an_expired_task_returns_terminal_public_error(self):
        await self.enqueue()
        stale = await self.db.get_async_task("task")
        await self.make_overdue()
        with patch.object(routes, "generation_handler", self.handler()):
            payload = await routes._build_queued_video_task_payload(stale)
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["progress"], 100)
        self.assertEqual(payload["error"], {"code":"QUEUE_TIMEOUT", "message":QUEUE_TIMEOUT_MESSAGE})
        self.assertNotIn("queue_position", payload)

    async def test_http_polling_aliases_report_failed_without_calling_upstream(self):
        await self.enqueue()
        await self.make_overdue()
        handler = self.handler()
        handler.get_video_task_payload = AsyncMock(return_value=None)
        app = FastAPI()
        app.include_router(routes.router)
        app.dependency_overrides[routes.verify_api_key_flexible] = lambda: "test-only"
        with patch.object(routes, "generation_handler", handler):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                for path in ("/v1/videos/task", "/api/v3/contents/generations/tasks/task"):
                    response = await client.get(path)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json()["status"], "failed")
                    self.assertEqual(response.json()["error"]["code"], "QUEUE_TIMEOUT")

    async def test_background_sweeper_runs_without_available_submit_workers(self):
        await self.enqueue()
        await self.make_overdue()
        with patch.object(routes, "generation_handler", self.handler()), \
             patch.object(routes.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await routes._run_async_task_queue_expirer()
        self.assertEqual((await self.db.get_async_task("task"))["status"], "failed")

    async def test_migration_backfills_original_created_time_without_extending_deadline(self):
        async with self.db._connect(write=True) as connection:
            await connection.execute("DROP TABLE async_task_queue")
            await connection.execute("""CREATE TABLE async_task_queue (
                id INTEGER PRIMARY KEY, task_id TEXT UNIQUE, task_type TEXT DEFAULT 'video',
                model TEXT, prompt TEXT, request_payload TEXT, base_url_override TEXT,
                status TEXT, upstream_task_id TEXT, attempt_count INTEGER DEFAULT 0,
                next_attempt_at REAL DEFAULT 0, last_error TEXT,
                created_at TIMESTAMP, updated_at TIMESTAMP)""")
            await connection.execute("INSERT INTO async_task_queue (task_id,status,created_at,request_payload) VALUES ('legacy','queued','2000-01-01 00:00:00','{}')")
            await connection.execute("INSERT OR REPLACE INTO generation_config (id,video_timeout) VALUES (1,600)")
            await connection.commit()
        await self.db.check_and_migrate_db()
        expected = datetime(2000,1,1,tzinfo=timezone.utc).timestamp() + 600
        self.assertEqual((await self.db.get_async_task("legacy"))["expires_at"], expected)
        await self.db.check_and_migrate_db()
        self.assertEqual((await self.db.get_async_task("legacy"))["expires_at"], expected)
        self.assertEqual(await self.db.expire_async_tasks(), 1)

    async def test_concurrent_sweep_and_admission_cannot_submit_overdue_task(self):
        await self.enqueue()
        await self.db.claim_next_async_task()
        await self.make_overdue()
        other = Database(db_path=self.db.db_path)
        admitted, _ = await asyncio.gather(
            other.admit_async_task_submission("task"), self.db.expire_async_tasks(),
        )
        self.assertFalse(admitted)
        self.assertEqual((await self.db.get_async_task("task"))["status"], "failed")

    async def test_submission_guard_is_isolated_between_concurrent_tasks(self):
        blocked = AsyncMock(return_value=False)
        allowed = AsyncMock(return_value=True)
        async def run(guard):
            with queue_submission_guard(guard):
                await asyncio.sleep(0)
                try:
                    await admit_queued_video_submission()
                    return True
                except AsyncQueueExpired:
                    return False
        self.assertEqual(await asyncio.gather(run(blocked), run(allowed)), [False, True])
        blocked.assert_awaited_once()
        allowed.assert_awaited_once()

    def test_timeout_is_bounded_and_never_infinite(self):
        for value, expected in [(None,1500), (float('inf'),1500), ('bad',1500), (0,60), (-1,60), (9000,7200), (600,600)]:
            self.assertEqual(normalize_queue_timeout(value), expected)
