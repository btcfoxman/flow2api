import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.api import routes


class PublicUnavailableErrorTests(unittest.IsolatedAsyncioTestCase):
    async def test_deferred_video_task_is_queued_without_preselecting_an_account(self):
        handler = SimpleNamespace(
            db=SimpleNamespace(
                enqueue_async_task=AsyncMock(
                    return_value={"position": 1, "capacity": 50}
                )
            ),
            load_balancer=SimpleNamespace(
                select_token=AsyncMock(return_value=None),
            )
        )
        request = routes.NormalizedGenerationRequest(
            model="abra_t2v_10s",
            prompt="hello",
            images=[],
        )

        with patch.object(routes, "generation_handler", handler):
            payload = await routes._create_deferred_async_video_task(request)

        self.assertEqual(payload["status"], "queued")
        self.assertEqual(payload["queue_position"], 1)
        self.assertEqual(payload["queue_capacity"], 50)
        handler.load_balancer.select_token.assert_not_awaited()

    async def test_queued_video_payload_hides_internal_proxy_cooldown(self):
        handler = SimpleNamespace(
            db=SimpleNamespace(get_async_task_position=AsyncMock(return_value=1))
        )
        queue_item = {
            "task_id": "flow2api-submit-test",
            "model": "abra_t2v_10s",
            "status": "queued",
            "last_error": "当前代理出口正在风险冷却，请约 4962 秒后重试。",
            "created_at_epoch": 123,
        }

        with patch.object(routes, "generation_handler", handler):
            payload = await routes._build_queued_video_task_payload(queue_item)

        self.assertEqual(payload["status"], "queued")
        self.assertNotIn("4962", str(payload))
        self.assertNotIn("代理", str(payload))

    async def test_queue_full_returns_friendly_429(self):
        handler = SimpleNamespace(
            db=SimpleNamespace(enqueue_async_task=AsyncMock(return_value=None))
        )
        request = routes.NormalizedGenerationRequest(
            model="abra_t2v_10s",
            prompt="hello",
            images=[],
        )

        with patch.object(routes, "generation_handler", handler):
            payload = await routes._create_deferred_async_video_task(request)

        self.assertEqual(payload["error"]["status_code"], 429)
        self.assertEqual(payload["error"]["code"], "task_queue_full")


if __name__ == "__main__":
    unittest.main()
