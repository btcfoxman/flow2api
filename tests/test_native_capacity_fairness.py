"""Browser-capacity admission is FIFO and never evicts busy accounts."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.services.browser_captcha_native_cdp import BrowserCaptchaService


class NativeCapacityFairnessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.service = BrowserCaptchaService(None)
        self.service._browser_limit = lambda: 1
        self.order = []
        self.tasks = []

    async def asyncTearDown(self):
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.service._reaper_task.cancel()
        await self.service._reaper_task

    def worker(self, token_id, *, running=False, busy=True):
        worker = SimpleNamespace(token_id=token_id, is_running=running,
                                 is_busy=busy, last_used_at=token_id)

        async def start():
            self.assertLess(len(self.service._running_workers()), self.service._browser_limit())
            worker.is_running = True
            self.order.append(token_id)

        async def stop(**_):
            self.assertFalse(worker.is_busy, "Must not evict a busy account")
            worker.is_running = False

        worker.start, worker.stop = AsyncMock(side_effect=start), AsyncMock(side_effect=stop)
        self.service._workers[token_id] = worker
        return worker

    def submit(self, worker):
        task = asyncio.create_task(self.service._ensure_capacity(worker))
        self.tasks.append(task)
        return task

    async def wait_queued(self, count):
        for _ in range(100):
            if self.service._queued == count:
                return
            await asyncio.sleep(0)
        self.fail(f"Expected {count} capacity waiters")

    async def notify(self):
        async with self.service._capacity_condition:
            self.service._capacity_condition.notify_all()

    async def test_new_probe_cannot_jump_a_waiting_generation(self):
        occupied = self.worker(1, running=True)
        generation, background = self.worker(2), self.worker(3)
        first = self.submit(generation)
        await self.wait_queued(1)
        occupied.is_busy = False
        # Deliberately let the new arrival run before notifying the older waiter.
        second = self.submit(background)
        await self.wait_queued(2)
        self.assertEqual(self.order, [])
        await self.notify()
        await asyncio.wait_for(first, 1)
        self.assertEqual(self.order, [2])
        self.assertFalse(second.done())
        generation.is_busy = False
        await self.notify()
        await asyncio.wait_for(second, 1)
        self.assertEqual(self.order, [2, 3])
        self.assertEqual(self.service._queued, 0)
        self.assertEqual(len(self.service._capacity_waiters), 0)

    async def test_cancelled_head_releases_next_waiter(self):
        occupied = self.worker(1, running=True)
        first = self.submit(self.worker(2))
        second = self.submit(self.worker(3))
        await self.wait_queued(2)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        occupied.is_busy = False
        await self.notify()
        await asyncio.wait_for(second, 1)
        self.assertEqual(self.order, [3])
        self.assertEqual(self.service._queued, 0)

    async def test_start_failure_does_not_stall_following_waiter(self):
        occupied = self.worker(1, running=True)
        failed, healthy = self.worker(2), self.worker(3)
        failed.start.side_effect = RuntimeError("simulated startup failure")
        first, second = self.submit(failed), self.submit(healthy)
        await self.wait_queued(2)
        occupied.is_busy = False
        await self.notify()
        with self.assertRaisesRegex(RuntimeError, "simulated startup failure"):
            await asyncio.wait_for(first, 1)
        await asyncio.wait_for(second, 1)
        self.assertEqual(self.order, [3])
        self.assertEqual(len(self.service._capacity_waiters), 0)

    async def test_existing_running_account_never_waits_behind_capacity_queue(self):
        occupied = self.worker(1, running=True)
        pending = self.submit(self.worker(2))
        await self.wait_queued(1)
        await asyncio.wait_for(self.service._ensure_capacity(occupied), 0.2)
        self.assertFalse(pending.done())
        occupied.stop.assert_not_awaited()

    async def test_concurrent_same_account_starts_only_once(self):
        worker = self.worker(1)
        entered, release = asyncio.Event(), asyncio.Event()
        real_start = worker.start.side_effect

        async def slow_start():
            entered.set()
            await release.wait()
            await real_start()

        worker.start.side_effect = slow_start
        first = self.submit(worker)
        await asyncio.wait_for(entered.wait(), 1)
        second = self.submit(worker)
        await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(asyncio.gather(first, second), 1)
        worker.start.assert_awaited_once()
        self.assertEqual(len(self.service._capacity_waiters), 0)

    async def test_multi_slot_capacity_limit_is_preserved(self):
        self.service._browser_limit = lambda: 2
        workers = [self.worker(i) for i in range(1, 4)]
        tasks = [self.submit(worker) for worker in workers]
        await asyncio.wait_for(asyncio.gather(*tasks[:2]), 1)
        await self.wait_queued(1)
        self.assertEqual(self.order, [1, 2])
        workers[0].is_busy = False
        await self.notify()
        await asyncio.wait_for(tasks[2], 1)
        self.assertEqual(self.order, [1, 2, 3])
        self.assertEqual(len(self.service._running_workers()), 2)

    async def test_closed_service_does_not_leave_waiters_or_start_browsers(self):
        self.worker(1, running=True)
        worker = self.worker(2)
        pending = self.submit(worker)
        await self.wait_queued(1)
        self.service._closed = True
        await self.notify()
        with self.assertRaisesRegex(RuntimeError, "service is closed"):
            await asyncio.wait_for(pending, 1)
        worker.start.assert_not_awaited()
        self.assertEqual(self.service._queued, 0)
        self.assertEqual(len(self.service._capacity_waiters), 0)

    async def test_service_closed_while_waiting_for_lock_never_starts_browser(self):
        worker = self.worker(1)
        await self.service._capacity_lock.acquire()
        pending = self.submit(worker)
        await asyncio.sleep(0)
        self.service._closed = True
        self.service._capacity_lock.release()
        with self.assertRaisesRegex(RuntimeError, "service is closed"):
            await asyncio.wait_for(pending, 1)
        worker.start.assert_not_awaited()
        self.assertEqual(len(self.service._capacity_waiters), 0)


if __name__ == "__main__":
    unittest.main()
