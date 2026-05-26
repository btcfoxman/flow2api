import json
import tempfile
import unittest

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


if __name__ == "__main__":
    unittest.main()
