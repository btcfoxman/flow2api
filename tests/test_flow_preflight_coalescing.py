"""Cold-start probes must be bounded, revision-aware, and cancellation-safe."""
import asyncio
import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.core.config import config
from src.core.generation_errors import NativeSessionError
from src.core.models import Token
from src.core.session_availability import SessionAvailability
from src.services.load_balancer import LoadBalancer
from src.services.token_manager import TokenManager


def account(token_id=1, **overrides):
    fields = dict(id=token_id, st=f"flow:{token_id}", email=f"user{token_id}@example.com",
                  auth_mode="flow", is_active=True, credits=100,
                  captcha_proxy_url=f"socks5://127.0.0.1:{20000 + token_id}",
                  google_cookies=json.dumps([
                      {"name": "SID", "value": "test-root", "domain": ".google.com"},
                      {"name": "OSID", "value": "test-flow", "domain": "flow.google.com"}]))
    return Token(**{**fields, **overrides})


SNAPSHOT = {"credits": 80, "userPaygateTier": "PAYGATE_TIER_ONE",
            "projects": [{"project_id": "test-project"}]}


class FlowPreflightTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        previous = config.captcha_method
        config.set_captcha_method("native_cdp")
        self.addCleanup(config.set_captcha_method, previous)
        self.tokens = {1: account()}
        self.token = self.tokens[1]
        self.db = SimpleNamespace(
            get_token=AsyncMock(side_effect=lambda token_id: self.tokens.get(token_id)),
            get_active_tokens=AsyncMock(side_effect=lambda: [t for t in self.tokens.values() if t.is_active]),
            update_token=AsyncMock(), get_projects_by_token=AsyncMock(return_value=[]),
            delete_token=AsyncMock(side_effect=lambda token_id: self.tokens.pop(token_id, None)))
        self.manager = TokenManager(self.db, SimpleNamespace())
        self.started, self.release, self.cleaned = asyncio.Event(), asyncio.Event(), asyncio.Event()
        self.service = SimpleNamespace(flow_account_snapshot=AsyncMock(side_effect=self.probe),
                                       remove_token=AsyncMock())
        patcher = patch("src.services.browser_captcha_native_cdp.BrowserCaptchaService.get_instance",
                        AsyncMock(return_value=self.service))
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch("src.core.native_session_state.local_session_state", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def probe(self, *_):
        self.started.set()
        try:
            await self.release.wait()
            return SNAPSHOT
        finally:
            self.cleaned.set()

    async def wait_for_readers(self, count):
        for _ in range(100):
            if sum(self.manager._flow_account_waiters.values()) == count:
                return
            await asyncio.sleep(0)
        self.fail(f"Expected {count} shared probe readers")

    def assert_no_probes(self):
        self.assertEqual(self.manager._flow_account_reads, {})
        self.assertEqual(self.manager._flow_account_waiters, {})

    async def test_scheduler_balance_and_manual_check_share_one_inflight_probe(self):
        scheduler = asyncio.create_task(self.manager.ensure_valid_token(self.token))
        await asyncio.wait_for(self.started.wait(), 1)
        balance = asyncio.create_task(self.manager.refresh_credits(1))
        manual = asyncio.create_task(self.manager.verify_native_session(1))
        await self.wait_for_readers(3)
        self.release.set()
        selected, credits, verified = await asyncio.gather(scheduler, balance, manual)
        self.assertEqual(selected.id, 1)
        self.assertEqual(credits, 80)
        self.assertTrue(verified)
        self.service.flow_account_snapshot.assert_awaited_once()
        self.db.update_token.assert_awaited_once()
        self.assert_no_probes()

    async def test_failed_probe_is_not_repeated_by_waiting_schedulers(self):
        async def rejected(*args):
            await self.probe(*args)
            raise NativeSessionError("flow_login_unavailable")

        self.service.flow_account_snapshot.side_effect = rejected
        tasks = [asyncio.create_task(self.manager.ensure_valid_token(self.token)) for _ in range(4)]
        await asyncio.wait_for(self.started.wait(), 1)
        balance = asyncio.create_task(self.manager.refresh_credits(1))
        await self.wait_for_readers(2)
        self.release.set()
        self.assertEqual(await asyncio.gather(*tasks), [None] * 4)
        await balance
        self.service.flow_account_snapshot.assert_awaited_once()
        self.assertFalse(self.manager.native_sessions.available(self.token))
        self.assert_no_probes()

    async def test_manual_verification_still_probes_fresh_and_blocked_sessions(self):
        self.release.set()
        self.assertIsNotNone(await self.manager.ensure_valid_token(self.token))
        self.assertTrue(await self.manager.verify_native_session(1))
        self.manager.native_sessions.reject(self.token, NativeSessionError("flow_login_unavailable"))
        self.assertTrue(await self.manager.verify_native_session(1))
        self.assertTrue(self.manager.native_sessions.available(self.token))
        self.assertEqual(self.service.flow_account_snapshot.await_count, 3)

    async def test_one_cancelled_waiter_does_not_cancel_other_probe_users(self):
        first = asyncio.create_task(self.manager._read_flow_account(self.token))
        await asyncio.wait_for(self.started.wait(), 1)
        second = asyncio.create_task(self.manager._read_flow_account(self.token))
        await self.wait_for_readers(2)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.assertFalse(self.cleaned.is_set())
        self.release.set()
        self.assertEqual(await second, SNAPSHOT)
        self.service.flow_account_snapshot.assert_awaited_once()
        self.assert_no_probes()

    async def test_last_cancelled_waiter_awaits_cleanup(self):
        task = asyncio.create_task(self.manager._read_flow_account(self.token))
        await asyncio.wait_for(self.started.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(self.cleaned.is_set())
        self.db.update_token.assert_not_awaited()
        self.assert_no_probes()

    async def test_deleting_account_cancels_probe_before_browser_removal(self):
        task = asyncio.create_task(self.manager._read_flow_account(self.token))
        await asyncio.wait_for(self.started.wait(), 1)
        await self.manager.delete_token(1)
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(self.cleaned.is_set())
        self.service.remove_token.assert_awaited_once_with(1)
        self.db.update_token.assert_not_awaited()
        self.assert_no_probes()

    async def test_credential_replacement_does_not_share_or_reject_new_revision(self):
        old = asyncio.create_task(self.manager.ensure_valid_token(self.token))
        await asyncio.wait_for(self.started.wait(), 1)
        replacement = account(captcha_proxy_url="socks5://127.0.0.1:20111")
        self.tokens[1] = replacement
        new = asyncio.create_task(self.manager._read_flow_account(replacement))
        await self.wait_for_readers(2)
        self.release.set()
        self.assertIsNone(await old)
        self.assertEqual(await new, SNAPSHOT)
        self.assertEqual(self.service.flow_account_snapshot.await_count, 2)
        self.db.update_token.assert_awaited_once()
        self.assertTrue(self.manager.native_sessions.available(replacement))
        self.assertTrue(self.manager._flow_verification_fresh(replacement))
        self.assert_no_probes()

    async def test_removed_disabled_or_replaced_candidate_is_not_probed_after_lock(self):
        for state in ("removed", "disabled", "replaced"):
            with self.subTest(state=state):
                self.tokens[1] = self.token
                lock = await self.manager._get_token_lock(
                    self.manager._refresh_locks, self.manager._refresh_lock_guard, 1)
                await lock.acquire()
                task = asyncio.create_task(self.manager.ensure_valid_token(self.token))
                await asyncio.sleep(0)
                if state == "removed":
                    self.tokens.pop(1)
                else:
                    self.tokens[1] = account(**({"is_active": False} if state == "disabled"
                                               else {"google_cookies": "new-snapshot"}))
                lock.release()
                self.assertIsNone(await task)
        self.service.flow_account_snapshot.assert_not_awaited()

    async def test_periodic_waiters_recheck_rejected_removed_and_disabled_accounts(self):
        self.tokens.update({i: account(i) for i in range(2, 5)})

        async def refresh(token_id):
            self.assertEqual(token_id, 1)
            self.manager.native_sessions.reject(self.tokens[2], NativeSessionError("flow_login_unavailable"))
            self.tokens.pop(3)
            self.tokens[4].is_active = False
            return True, 80

        self.manager._refresh_credits_with_status = AsyncMock(side_effect=refresh)
        summary = await self.manager.refresh_all_active_credits(concurrency=1)
        self.assertEqual(summary, {"total": 4, "succeeded": 1, "failed": 0, "skipped": 3})
        self.manager._refresh_credits_with_status.assert_awaited_once_with(1)

    async def test_periodic_startup_prioritizes_recent_accounts_with_bounded_concurrency(self):
        self.tokens.update({2: account(2, last_used_at=datetime(2026, 9, 13)),
                            3: account(3, last_used_at=datetime(2026, 9, 14, tzinfo=timezone.utc))})
        order, concurrent, maximum = [], 0, 0

        async def refresh(token_id):
            nonlocal concurrent, maximum
            order.append(token_id)
            concurrent += 1
            maximum = max(maximum, concurrent)
            await asyncio.sleep(0)
            concurrent -= 1
            return True, 80

        self.manager._refresh_credits_with_status = AsyncMock(side_effect=refresh)
        summary = await self.manager.refresh_all_active_credits(concurrency=2)
        self.assertEqual(order, [3, 2, 1])
        self.assertEqual(maximum, 2)
        self.assertEqual(summary["succeeded"], 3)

    async def test_periodic_stop_cleans_its_probe(self):
        self.manager.start_periodic_credits_refresh(interval_seconds=3600, concurrency=1)
        await asyncio.wait_for(self.started.wait(), 1)
        await self.manager.stop_periodic_credits_refresh()
        self.assertTrue(self.cleaned.is_set())
        self.assert_no_probes()
        self.assertEqual(self.manager._credits_refresh_futures, {})

    async def test_periodic_stop_does_not_cancel_probe_shared_with_scheduler(self):
        self.manager.start_periodic_credits_refresh(interval_seconds=3600, concurrency=1)
        await asyncio.wait_for(self.started.wait(), 1)
        task = asyncio.create_task(self.manager.ensure_valid_token(self.token))
        await self.wait_for_readers(2)
        await self.manager.stop_periodic_credits_refresh()
        self.assertFalse(task.done())
        self.release.set()
        self.assertEqual((await task).id, 1)
        self.assert_no_probes()

    async def test_different_accounts_do_not_share_probe_or_credentials(self):
        self.tokens[2] = account(2)
        tasks = [asyncio.create_task(self.manager._read_flow_account(token))
                 for token in self.tokens.values()]
        await self.wait_for_readers(2)
        self.release.set()
        self.assertEqual(await asyncio.gather(*tasks), [SNAPSHOT, SNAPSHOT])
        self.assertEqual(self.service.flow_account_snapshot.await_count, 2)
        self.assertEqual({call.args for call in self.service.flow_account_snapshot.await_args_list},
                         {(token.id, token.email) for token in self.tokens.values()})
        self.assert_no_probes()

    async def test_queued_periodic_probe_uses_replaced_credentials(self):
        self.tokens[2] = account(2)
        self.manager.native_sessions.reject(self.tokens[2], NativeSessionError("flow_login_unavailable"))
        # Admit a locally repaired revision at round creation, then replace it
        # once more while its semaphore slot is waiting.
        self.tokens[2] = account(2, google_cookies="replacement-1")

        async def refresh(token_id):
            if token_id == 1:
                self.tokens[2] = account(2, google_cookies="replacement-2")
            else:
                self.assertEqual(self.tokens[token_id].google_cookies, "replacement-2")
            return True, 80

        self.manager._refresh_credits_with_status = AsyncMock(side_effect=refresh)
        summary = await self.manager.refresh_all_active_credits(concurrency=1)
        self.assertEqual(summary, {"total": 2, "succeeded": 2, "failed": 0, "skipped": 0})

    async def test_scheduler_prioritizes_cold_recent_session_but_preserves_warm_round_robin(self):
        previous = config.call_logic_mode
        config.set_call_logic_mode("polling")
        self.addCleanup(config.set_call_logic_mode, previous)
        self.tokens[2] = account(2, last_used_at=datetime(2026, 9, 14))
        self.release.set()
        balancer = LoadBalancer(self.manager)
        selected = await balancer.select_token()
        self.assertEqual(selected.id, 2)
        self.service.flow_account_snapshot.assert_awaited_once_with(2, self.tokens[2].email)
        await self.manager.ensure_valid_token(self.token)
        selected = [(await balancer.select_token()).id for _ in range(4)]
        self.assertEqual(selected, [2, 1, 2, 1])

    async def test_recent_account_does_not_bypass_proxy_cooldown(self):
        self.tokens[2] = account(2, last_used_at=datetime(2026, 9, 14))
        self.release.set()
        balancer = LoadBalancer(self.manager)
        balancer._get_native_video_proxy_state = AsyncMock(side_effect=lambda token: {
            "available": token.id != 2, "proxy_key": str(token.id), "cooldown_remaining_seconds": 300})
        selected = await balancer.select_token(for_video_generation=True)
        self.assertEqual(selected.id, 1)
        self.service.flow_account_snapshot.assert_awaited_once_with(1, self.token.email)


class PreflightPriorityTests(unittest.TestCase):
    def test_only_unknown_flow_slots_move_and_equal_recency_is_stable(self):
        sessions = SessionAvailability()
        tokens = [account(1), account(2, auth_mode="labs"), account(3),
                  account(4, last_used_at=datetime(2026, 9, 13)),
                  account(5, last_used_at=datetime(2026, 9, 14)), account(6)]
        sessions.verified(tokens[2])
        self.assertEqual([t.id for t in sessions.prioritize_preflight(tokens)], [5, 2, 3, 4, 1, 6])
        self.assertEqual([t.id for t in tokens], [1, 2, 3, 4, 5, 6])


if __name__ == "__main__":
    unittest.main()
