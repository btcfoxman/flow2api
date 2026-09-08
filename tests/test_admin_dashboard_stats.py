import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.api import admin


class AdminDashboardStatsTests(unittest.IsolatedAsyncioTestCase):
    async def test_stats_include_success_totals_and_async_queue_state(self):
        dashboard_stats = {
            "total_tokens": 8,
            "active_tokens": 6,
            "total_images": 11,
            "total_videos": 7,
            "total_errors": 5,
            "today_images": 3,
            "today_videos": 2,
            "today_errors": 1,
        }
        queue_stats = {
            "queued": 4,
            "submitting": 1,
            "failed": 2,
            "active": 5,
            "oldest_created_at": 1788740000,
        }
        outcome_stats = {
            "total_images": 9,
            "total_videos": 4,
            "total_successes": 13,
            "total_failed_tasks": 6,
            "today_images": 2,
            "today_videos": 1,
            "today_successes": 3,
            "today_failed_tasks": 2,
        }
        fake_db = SimpleNamespace(
            get_dashboard_stats=AsyncMock(return_value=dashboard_stats),
            get_generation_outcome_stats=AsyncMock(return_value=outcome_stats),
            get_async_task_queue_stats=AsyncMock(return_value=queue_stats),
            get_generation_config=AsyncMock(
                return_value=SimpleNamespace(async_task_queue_capacity=75)
            ),
        )

        with patch.object(admin, "db", fake_db):
            result = await admin.get_stats(token="admin-session")

        self.assertEqual(result["today_successes"], 3)
        self.assertEqual(result["total_successes"], 13)
        self.assertEqual(result["today_failed_tasks"], 2)
        self.assertEqual(result["total_failed_tasks"], 6)
        self.assertEqual(result["total_videos"], 4)
        self.assertEqual(
            result["async_task_queue"],
            {
                **queue_stats,
                "capacity": 75,
            },
        )
        self.assertEqual(result["total_tokens"], 8)


if __name__ == "__main__":
    unittest.main()
