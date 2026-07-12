import json
import tempfile
import unittest
from datetime import datetime, timezone

from src.core.database import Database
from src.core.models import RequestLog, Task, Token


class AsyncVideoResultBackfillTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(db_path=f"{self._temp_dir.name}/flow.db")
        await self.db.init_db()
        self.token_id = await self.db.add_token(
            Token(
                st="st-test",
                at="at-test",
                email="tester@example.com",
                name="tester",
            )
        )

    async def asyncTearDown(self):
        self._temp_dir.cleanup()

    async def test_backfills_missing_async_failure_result_log_once(self):
        await self.db.create_task(
            Task(
                task_id="task-failed",
                token_id=self.token_id,
                model="abra_r2v_10s",
                prompt="hello",
                status="failed",
                progress=45,
                error_message="PUBLIC_ERROR_UNSAFE_GENERATION (code: 3)",
            )
        )
        await self.db.update_task(
            "task-failed",
            error_message="PUBLIC_ERROR_UNSAFE_GENERATION (code: 3)",
        )
        await self.db.add_request_log(
            RequestLog(
                token_id=self.token_id,
                operation="generate_video",
                request_body=json.dumps({"prompt": "hello"}),
                response_body=json.dumps({"status": "processing", "task_id": "task-failed"}),
                status_code=200,
                duration=1.0,
                status_text="video_submitted",
                progress=45,
            )
        )

        inserted = await self.db.backfill_async_video_result_logs()
        inserted_again = await self.db.backfill_async_video_result_logs()

        logs = await self.db.get_logs(include_payload=True)
        async_logs = [log for log in logs if log["operation"] == "generate_video_async_result"]
        response_body = json.loads(async_logs[0]["response_body"])

        self.assertEqual(inserted, 1)
        self.assertEqual(inserted_again, 0)
        self.assertEqual(len(async_logs), 1)
        self.assertEqual(async_logs[0]["status_code"], 400)
        self.assertEqual(async_logs[0]["status_text"], "failed")
        self.assertEqual(async_logs[0]["progress"], 100)
        self.assertEqual(response_body["task_id"], "task-failed")
        self.assertNotIn("PUBLIC_ERROR_UNSAFE_GENERATION", response_body["error"])
        self.assertIn("内容安全策略", response_body["error"])

    async def test_backfill_skips_sync_completed_logs(self):
        await self.db.create_task(
            Task(
                task_id="task-sync",
                token_id=self.token_id,
                model="abra_t2v_10s",
                prompt="hello",
                status="completed",
                progress=100,
                result_urls=["https://example.com/video.mp4"],
            )
        )
        await self.db.add_request_log(
            RequestLog(
                token_id=self.token_id,
                operation="generate_video",
                request_body=json.dumps({"prompt": "hello"}),
                response_body=json.dumps({"status": "completed", "task_id": "task-sync"}),
                status_code=200,
                duration=2.0,
                status_text="completed",
                progress=100,
            )
        )

        inserted = await self.db.backfill_async_video_result_logs()

        self.assertEqual(inserted, 0)

    async def test_backfills_failed_post_submit_task_when_submit_log_is_missing(self):
        await self.db.create_task(
            Task(
                task_id="task-cleared-log",
                token_id=self.token_id,
                model="abra_r2v_10s",
                prompt="hello",
                status="failed",
                progress=45,
                operations=[{"operation": {"name": "task-cleared-log"}}],
            )
        )
        await self.db.update_task(
            "task-cleared-log",
            error_message="视频生成被上游内容安全策略拒绝，请调整提示词或参考图后重试",
        )

        inserted = await self.db.backfill_async_video_result_logs()

        logs = await self.db.get_logs(include_payload=True)
        async_logs = [log for log in logs if log["operation"] == "generate_video_async_result"]
        response_body = json.loads(async_logs[0]["response_body"])

        self.assertEqual(inserted, 1)
        self.assertEqual(len(async_logs), 1)
        self.assertEqual(response_body["task_id"], "task-cleared-log")
        self.assertEqual(response_body["status"], "failed")

    async def test_backfill_does_not_recreate_logs_cleared_after_task_finished(self):
        await self.db.create_task(
            Task(
                task_id="task-before-clear",
                token_id=self.token_id,
                model="abra_r2v_10s",
                prompt="hello",
                status="failed",
                progress=100,
                operations=[{"operation": {"name": "task-before-clear"}}],
            )
        )
        await self.db.update_task(
            "task-before-clear",
            error_message="video failed",
            completed_at=datetime.now(timezone.utc).timestamp(),
        )

        await self.db.clear_all_logs()
        inserted = await self.db.backfill_async_video_result_logs()

        logs = await self.db.get_logs(include_payload=True)
        self.assertEqual(inserted, 0)
        self.assertEqual(logs, [])

    async def test_backfill_keeps_tasks_completed_after_log_clear(self):
        await self.db.clear_all_logs()
        await self.db.create_task(
            Task(
                task_id="task-after-clear",
                token_id=self.token_id,
                model="abra_r2v_10s",
                prompt="hello",
                status="failed",
                progress=100,
                operations=[{"operation": {"name": "task-after-clear"}}],
            )
        )
        await self.db.update_task(
            "task-after-clear",
            error_message="video failed",
            completed_at=datetime.now(timezone.utc).timestamp() + 1,
        )

        inserted = await self.db.backfill_async_video_result_logs()

        logs = await self.db.get_logs(include_payload=True)
        async_logs = [log for log in logs if log["operation"] == "generate_video_async_result"]
        self.assertEqual(inserted, 1)
        self.assertEqual(len(async_logs), 1)

    async def test_backfill_duration_treats_sqlite_timestamp_as_utc(self):
        created_at = "2026-05-28 08:12:40"
        completed_at = datetime(2026, 5, 28, 8, 12, 47, 250000, tzinfo=timezone.utc).timestamp()

        duration = self.db._task_duration_seconds(created_at, completed_at)

        self.assertAlmostEqual(duration, 7.25, places=2)

    async def test_normalizes_finished_task_progress(self):
        await self.db.create_task(
            Task(
                task_id="task-failed-progress",
                token_id=self.token_id,
                model="abra_r2v_10s",
                prompt="hello",
                status="failed",
                progress=45,
            )
        )
        await self.db.create_task(
            Task(
                task_id="task-processing-progress",
                token_id=self.token_id,
                model="abra_r2v_10s",
                prompt="hello",
                status="processing",
                progress=45,
            )
        )

        updated = await self.db.normalize_finished_task_progress()

        failed_task = await self.db.get_task("task-failed-progress")
        processing_task = await self.db.get_task("task-processing-progress")
        self.assertEqual(updated, 1)
        self.assertEqual(failed_task.progress, 100)
        self.assertEqual(processing_task.progress, 45)

    async def test_fails_only_stale_processing_tasks(self):
        await self.db.create_task(
            Task(
                task_id="task-stale-processing",
                token_id=self.token_id,
                model="abra_r2v_10s",
                prompt="hello",
                status="processing",
                progress=45,
            )
        )
        await self.db.create_task(
            Task(
                task_id="task-recent-processing",
                token_id=self.token_id,
                model="abra_r2v_10s",
                prompt="hello",
                status="processing",
                progress=45,
            )
        )
        async with self.db._connect(write=True) as db:
            await db.execute(
                "UPDATE tasks SET created_at = datetime('now', '-3 hours') WHERE task_id = ?",
                ("task-stale-processing",),
            )
            await db.commit()

        updated = await self.db.fail_stale_processing_tasks(stale_after_seconds=7200)

        stale_task = await self.db.get_task("task-stale-processing")
        recent_task = await self.db.get_task("task-recent-processing")
        self.assertEqual(updated, 1)
        self.assertEqual(stale_task.status, "failed")
        self.assertEqual(stale_task.progress, 100)
        self.assertIsNotNone(stale_task.completed_at)
        self.assertIn("超过恢复时限", stale_task.error_message)
        self.assertEqual(recent_task.status, "processing")
        self.assertEqual(recent_task.progress, 45)

    async def test_fails_only_stale_processing_image_logs(self):
        stale_log_id = await self.db.add_request_log(
            RequestLog(
                token_id=self.token_id,
                operation="generate_image",
                request_body=json.dumps({"prompt": "old"}),
                response_body=json.dumps({"status": "processing"}),
                status_code=102,
                duration=0,
                status_text="submitting_image",
                progress=48,
            )
        )
        recent_log_id = await self.db.add_request_log(
            RequestLog(
                token_id=self.token_id,
                operation="generate_image",
                request_body=json.dumps({"prompt": "recent"}),
                response_body=json.dumps({"status": "processing"}),
                status_code=102,
                duration=0,
                status_text="submitting_image",
                progress=48,
            )
        )
        async with self.db._connect(write=True) as db:
            await db.execute(
                "UPDATE request_logs SET created_at = datetime('now', '-3 hours') WHERE id = ?",
                (stale_log_id,),
            )
            await db.commit()

        updated = await self.db.fail_stale_processing_image_logs(7200)

        stale_log = await self.db.get_log_detail(stale_log_id)
        recent_log = await self.db.get_log_detail(recent_log_id)
        self.assertEqual(updated, 1)
        self.assertEqual(stale_log["status_code"], 499)
        self.assertEqual(stale_log["status_text"], "failed")
        self.assertEqual(stale_log["progress"], 100)
        self.assertIn("超过恢复时限", stale_log["response_body"])
        self.assertEqual(recent_log["status_code"], 102)
        self.assertEqual(recent_log["progress"], 48)


if __name__ == "__main__":
    unittest.main()
