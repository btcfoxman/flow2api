import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.core.config import Config
from src.core.models import Token
from src.services.token_manager import TokenManager


def make_token(token_id: int, credits: int = 100) -> Token:
    return Token(
        id=token_id,
        st=f"st-{token_id}",
        at=f"at-{token_id}",
        at_expires=datetime.now(timezone.utc) + timedelta(hours=12),
        email=f"token-{token_id}@example.com",
        credits=credits,
        is_active=True,
    )


class PeriodicCreditsRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_refreshes_for_same_token_share_one_request(self):
        token = make_token(1)
        request_started = asyncio.Event()
        release_request = asyncio.Event()

        async def get_credits(_access_token):
            request_started.set()
            await release_request.wait()
            return {"credits": 88, "userPaygateTier": "PAYGATE_TIER_ONE"}

        db = SimpleNamespace(
            get_token=AsyncMock(return_value=token),
            update_token=AsyncMock(),
        )
        flow_client = SimpleNamespace(get_credits=AsyncMock(side_effect=get_credits))
        manager = TokenManager(db, flow_client)

        first = asyncio.create_task(manager.refresh_credits(1))
        await asyncio.wait_for(request_started.wait(), timeout=1)
        second = asyncio.create_task(manager.refresh_credits(1))
        await asyncio.sleep(0)
        release_request.set()

        self.assertEqual(await first, 88)
        self.assertEqual(await second, 88)
        flow_client.get_credits.assert_awaited_once_with("at-1")
        db.update_token.assert_awaited_once_with(
            1,
            credits=88,
            user_paygate_tier="PAYGATE_TIER_ONE",
        )

    async def test_refresh_all_active_tokens_isolates_single_token_failure(self):
        tokens = [make_token(1), make_token(2), make_token(3)]
        db = SimpleNamespace(get_active_tokens=AsyncMock(return_value=tokens))
        manager = TokenManager(db, SimpleNamespace())

        async def refresh_with_status(token_id):
            if token_id == 2:
                raise RuntimeError("temporary failure")
            return True, 100 - token_id

        manager._refresh_credits_with_status = AsyncMock(
            side_effect=refresh_with_status
        )

        summary = await manager.refresh_all_active_credits(concurrency=2)

        self.assertEqual(summary, {"total": 3, "succeeded": 2, "failed": 1})
        self.assertEqual(
            sorted(call.args[0] for call in manager._refresh_credits_with_status.await_args_list),
            [1, 2, 3],
        )

    async def test_cancelled_removed_token_does_not_stop_refresh_round(self):
        tokens = [make_token(1), make_token(2)]
        db = SimpleNamespace(get_active_tokens=AsyncMock(return_value=tokens))
        manager = TokenManager(db, SimpleNamespace())

        async def refresh_with_status(token_id):
            if token_id == 1:
                raise asyncio.CancelledError()
            return True, 98

        manager._refresh_credits_with_status = AsyncMock(
            side_effect=refresh_with_status
        )

        summary = await manager.refresh_all_active_credits(concurrency=2)

        self.assertEqual(summary, {"total": 2, "succeeded": 1, "failed": 1})

    async def test_periodic_task_refreshes_immediately_and_stops_cleanly(self):
        manager = TokenManager(SimpleNamespace(), SimpleNamespace())
        first_round_started = asyncio.Event()

        async def refresh_round(concurrency=None):
            first_round_started.set()
            return {"total": 0, "succeeded": 0, "failed": 0}

        manager.refresh_all_active_credits = AsyncMock(side_effect=refresh_round)

        self.assertTrue(
            manager.start_periodic_credits_refresh(
                interval_seconds=3600,
                concurrency=2,
            )
        )
        first_task = manager._periodic_credits_refresh_task
        self.assertTrue(manager.start_periodic_credits_refresh(interval_seconds=60))
        self.assertIs(manager._periodic_credits_refresh_task, first_task)
        await asyncio.wait_for(first_round_started.wait(), timeout=1)

        await manager.stop_periodic_credits_refresh()

        manager.refresh_all_active_credits.assert_awaited_once_with(concurrency=2)
        self.assertIsNone(manager._periodic_credits_refresh_task)
        self.assertTrue(first_task.done())

    async def test_zero_interval_disables_periodic_task(self):
        manager = TokenManager(SimpleNamespace(), SimpleNamespace())

        self.assertFalse(manager.start_periodic_credits_refresh(interval_seconds=0))
        self.assertIsNone(manager._periodic_credits_refresh_task)


class PeriodicCreditsRefreshConfigTests(unittest.TestCase):
    def test_interval_and_concurrency_are_bounded(self):
        config = Config()
        flow_config = config._config.setdefault("flow", {})

        flow_config["credits_refresh_interval_seconds"] = 30
        self.assertEqual(config.credits_refresh_interval_seconds, 60)
        flow_config["credits_refresh_interval_seconds"] = 0
        self.assertEqual(config.credits_refresh_interval_seconds, 0)
        flow_config["credits_refresh_interval_seconds"] = "invalid"
        self.assertEqual(config.credits_refresh_interval_seconds, 900)

        flow_config["credits_refresh_concurrency"] = 100
        self.assertEqual(config.credits_refresh_concurrency, 20)
        flow_config["credits_refresh_concurrency"] = "invalid"
        self.assertEqual(config.credits_refresh_concurrency, 3)


if __name__ == "__main__":
    unittest.main()
