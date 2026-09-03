import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.api import routes


class PublicUnavailableErrorTests(unittest.IsolatedAsyncioTestCase):
    async def test_deferred_video_task_hides_internal_proxy_cooldown(self):
        handler = SimpleNamespace(
            load_balancer=SimpleNamespace(
                select_token=AsyncMock(return_value=None),
                get_unavailable_reason=AsyncMock(
                    return_value=(
                        "当前可用账号的代理出口正在风险冷却，"
                        "请约 4962 秒后重试。"
                    )
                ),
            )
        )
        request = routes.NormalizedGenerationRequest(
            model="abra_t2v_10s",
            prompt="hello",
            images=[],
        )

        with patch.object(routes, "generation_handler", handler):
            payload = await routes._create_deferred_async_video_task(request)

        self.assertEqual(payload["error"]["status_code"], 503)
        self.assertEqual(
            payload["error"]["message"],
            "视频生成服务暂时不可用，请稍后重试",
        )
        self.assertNotIn("4962", str(payload))
        self.assertNotIn("代理", str(payload))


if __name__ == "__main__":
    unittest.main()
