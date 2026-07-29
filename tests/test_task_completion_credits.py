import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.services.generation_handler import GenerationHandler


class VideoTaskCompletionCreditsTests(unittest.IsolatedAsyncioTestCase):
    async def test_completed_task_refreshes_the_used_token_credits_once(self):
        events = []

        async def update_task(task_id, **fields):
            events.append(("task", task_id, fields["status"]))

        async def refresh_credits(token_id):
            events.append(("credits", token_id))
            return 750

        handler = GenerationHandler.__new__(GenerationHandler)
        handler.db = SimpleNamespace(update_task=AsyncMock(side_effect=update_task))
        handler.token_manager = SimpleNamespace(
            refresh_credits=AsyncMock(side_effect=refresh_credits)
        )

        await handler._complete_video_task(
            task_id="task-1",
            token_id=17,
            result_urls=["https://example.com/video.mp4"],
        )

        self.assertEqual(
            events,
            [
                ("task", "task-1", "completed"),
                ("credits", 17),
            ],
        )
        handler.token_manager.refresh_credits.assert_awaited_once_with(17)
        handler.db.update_task.assert_awaited_once()
        update_fields = handler.db.update_task.await_args.kwargs
        self.assertEqual(update_fields["progress"], 100)
        self.assertEqual(
            update_fields["result_urls"],
            ["https://example.com/video.mp4"],
        )

    async def test_credit_refresh_failure_does_not_revert_completed_task(self):
        handler = GenerationHandler.__new__(GenerationHandler)
        handler.db = SimpleNamespace(update_task=AsyncMock())
        handler.token_manager = SimpleNamespace(
            refresh_credits=AsyncMock(side_effect=RuntimeError("upstream unavailable"))
        )

        await handler._complete_video_task(
            task_id="task-2",
            token_id=23,
            result_urls=["https://example.com/video.mp4"],
        )

        handler.db.update_task.assert_awaited_once()
        self.assertEqual(
            handler.db.update_task.await_args.kwargs["status"],
            "completed",
        )
        handler.token_manager.refresh_credits.assert_awaited_once_with(23)


if __name__ == "__main__":
    unittest.main()
