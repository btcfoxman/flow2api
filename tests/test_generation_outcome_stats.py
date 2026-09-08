import tempfile
import unittest

from src.core.database import Database
from src.core.models import RequestLog


class GenerationOutcomeStatsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(db_path=f"{self._temp_dir.name}/flow.db")
        await self.db.init_db()

    async def asyncTearDown(self):
        self._temp_dir.cleanup()

    async def _add_log(
        self,
        operation: str,
        status_text: str,
        status_code: int,
        progress: int,
    ) -> int:
        return await self.db.add_request_log(
            RequestLog(
                operation=operation,
                status_text=status_text,
                status_code=status_code,
                progress=progress,
                duration=1.0,
            )
        )

    async def test_only_terminal_completed_logs_are_successful(self):
        await self._add_log("generate_image", "completed", 200, 100)
        await self._add_log("generate_video", "video_submitted", 200, 45)
        await self._add_log("generate_video_async_result", "completed", 200, 100)
        await self._add_log("generate_image", "failed", 500, 100)

        old_success_id = await self._add_log("extend_video", "completed", 200, 100)
        old_failure_id = await self._add_log(
            "generate_video_async_result", "failed", 502, 100
        )
        await self._add_log("refresh_token", "failed", 500, 100)

        async with self.db._connect(write=True) as conn:
            await conn.execute(
                """
                UPDATE request_logs
                SET created_at = '2000-01-01 00:00:00',
                    updated_at = '2000-01-01 00:00:00'
                WHERE id IN (?, ?)
                """,
                (old_success_id, old_failure_id),
            )
            await self.db._upsert_generation_outcome_from_log(conn, old_success_id)
            await self.db._upsert_generation_outcome_from_log(conn, old_failure_id)
            await conn.commit()

        stats = await self.db.get_generation_outcome_stats()

        self.assertEqual(stats["total_images"], 1)
        self.assertEqual(stats["total_videos"], 2)
        self.assertEqual(stats["total_successes"], 3)
        self.assertEqual(stats["total_failed_tasks"], 2)
        self.assertEqual(stats["today_images"], 1)
        self.assertEqual(stats["today_videos"], 1)
        self.assertEqual(stats["today_successes"], 2)
        self.assertEqual(stats["today_failed_tasks"], 1)

        await self.db.clear_all_logs()
        after_log_cleanup = await self.db.get_generation_outcome_stats()
        self.assertEqual(after_log_cleanup, stats)

    async def test_existing_terminal_logs_are_backfilled_on_startup(self):
        async with self.db._connect(write=True) as conn:
            await conn.execute(
                """
                INSERT INTO request_logs (
                    operation, status_code, duration, status_text, progress
                ) VALUES ('generate_video', 200, 1.0, 'completed', 100)
                """
            )
            await conn.commit()

        await self.db.init_db()

        stats = await self.db.get_generation_outcome_stats()
        self.assertEqual(stats["total_videos"], 1)
        self.assertEqual(stats["total_successes"], 1)


if __name__ == "__main__":
    unittest.main()
