import asyncio
import copy
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.core.config import config
from src.services.flow_client import FlowClient


class FlowClientTrafficShapingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._original_flow_config = copy.deepcopy(config._config.get("flow", {}))

    def tearDown(self):
        config._config["flow"] = self._original_flow_config

    async def test_video_launch_limit_is_shared_across_accounts(self):
        config._config["flow"].update(
            {
                "video_launch_soft_limit": 1,
                "video_launch_wait_timeout": 1,
                "video_launch_stagger_ms": 0,
            }
        )
        client = FlowClient(None)

        first = await client._acquire_launch_gate(
            media_type="video",
            soft_limit=1,
            wait_timeout=1,
            stagger_ms=0,
        )
        self.assertTrue(first[0])

        second_task = asyncio.create_task(
            client._acquire_video_launch_gate(
                token_id=2,
                token_video_concurrency=20,
            )
        )
        await asyncio.sleep(0.02)
        self.assertFalse(second_task.done())

        await client._release_video_launch_gate(token_id=1)
        second = await asyncio.wait_for(second_task, timeout=1)
        self.assertTrue(second[0])
        await client._release_video_launch_gate(token_id=2)

    async def test_video_launches_are_staggered(self):
        client = FlowClient(None)

        first = await client._acquire_launch_gate(
            media_type="video",
            soft_limit=2,
            wait_timeout=1,
            stagger_ms=40,
        )
        self.assertTrue(first[0])

        started = time.monotonic()
        second = await client._acquire_launch_gate(
            media_type="video",
            soft_limit=2,
            wait_timeout=1,
            stagger_ms=40,
        )
        elapsed = time.monotonic() - started

        self.assertTrue(second[0])
        self.assertGreaterEqual(elapsed, 0.025)
        await client._release_video_launch_gate(token_id=1)
        await client._release_video_launch_gate(token_id=2)

    async def test_traffic_cooldown_delays_new_launch(self):
        client = FlowClient(None)
        client._traffic_cooldown_until = time.monotonic() + 0.04

        started = time.monotonic()
        result = await client._acquire_launch_gate(
            media_type="image",
            soft_limit=1,
            wait_timeout=1,
            stagger_ms=0,
        )
        elapsed = time.monotonic() - started

        self.assertTrue(result[0])
        self.assertGreaterEqual(elapsed, 0.025)
        await client._release_image_launch_gate(token_id=1)

    async def test_browser_fingerprint_retries_keep_the_same_egress(self):
        config._config["flow"].update(
            {
                "image_timeout_retry_count": 1,
                "image_timeout_retry_delay": 0,
                "image_timeout_use_media_proxy_fallback": True,
                "image_prefer_media_proxy": True,
            }
        )
        proxy_manager = SimpleNamespace(
            get_media_proxy_url=AsyncMock(return_value="http://media-proxy.example")
        )
        client = FlowClient(proxy_manager)
        client._set_request_fingerprint(
            {
                "browser_ref": 0,
                "proxy_url": "http://browser-egress.example",
            }
        )
        client._make_request = AsyncMock(
            side_effect=[TimeoutError("request timed out"), {"media": []}]
        )

        result = await client._make_image_generation_request(
            url="https://example.com/generate",
            json_data={"requests": []},
            at="access-token",
        )

        self.assertEqual(result, {"media": []})
        proxy_manager.get_media_proxy_url.assert_not_awaited()
        self.assertEqual(client._make_request.await_count, 2)
        for request_call in client._make_request.await_args_list:
            self.assertFalse(request_call.kwargs["use_media_proxy"])
            self.assertTrue(request_call.kwargs["respect_fingerprint_proxy"])

    async def test_default_user_agent_matches_tls_impersonation(self):
        client = FlowClient(None)

        self.assertIn("Chrome/124.0.6367.207", client._generate_user_agent("account-1"))


class TrafficCooldownConfigTests(unittest.TestCase):
    def setUp(self):
        self._original_flow_config = copy.deepcopy(config._config.get("flow", {}))

    def tearDown(self):
        config._config["flow"] = self._original_flow_config

    def test_traffic_cooldown_is_configurable_and_bounded(self):
        config._config["flow"]["traffic_cooldown_seconds"] = 0
        self.assertEqual(config.flow_traffic_cooldown_seconds, 0)

        config._config["flow"]["traffic_cooldown_seconds"] = 2
        self.assertEqual(config.flow_traffic_cooldown_seconds, 10)

        config._config["flow"]["traffic_cooldown_seconds"] = 5000
        self.assertEqual(config.flow_traffic_cooldown_seconds, 1800)


if __name__ == "__main__":
    unittest.main()
