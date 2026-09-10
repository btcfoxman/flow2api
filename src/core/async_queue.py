"""Queue expiry and the final, task-local admission boundary for video submits."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Awaitable, Callable


QUEUE_TIMEOUT_MESSAGE = "任务排队超时，已自动结束，不会继续生成。"
QUEUE_SUBMISSION_UNCERTAIN_MESSAGE = "任务提交结果未能确认，已停止自动重试，请核查任务结果。"


class AsyncQueueExpired(RuntimeError):
    def __init__(self):
        super().__init__(QUEUE_TIMEOUT_MESSAGE)


def normalize_queue_timeout(value) -> int:
    """Use the existing video timeout range; a queue must never wait forever."""
    try:
        return max(60, min(7200, int(value)))
    except (TypeError, ValueError, OverflowError):
        return 1500


_submission_guard = ContextVar("async_queue_submission_guard", default=None)


@dataclass
class _SubmissionGuard:
    check: Callable[[], Awaitable[bool]]
    admitted: bool = False


@contextmanager
def queue_submission_guard(guard):
    token = _submission_guard.set(_SubmissionGuard(guard))
    try:
        yield
    finally:
        _submission_guard.reset(token)


async def admit_queued_video_submission():
    """Atomically stop expired queued work before a chargeable upstream submit.

    Requests outside the persistent queue (including polling) have no guard.
    Once admitted, an upstream request may complete after the queue deadline;
    that is a generation timeout, not a reason to discard or resubmit its result.
    """
    guard = _submission_guard.get()
    if guard is not None and not guard.admitted:
        if not await guard.check():
            raise AsyncQueueExpired()
        guard.admitted = True
