import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.services.file_cache import FileCache


class AsyncDownloadFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_child_does_not_block_other_requests(self):
        job = asyncio.create_task(FileCache._run_download_command(
            [sys.executable, "-c", "import time; time.sleep(0.3)"], timeout=5))
        try:
            await asyncio.sleep(0.05)
            self.assertFalse(job.done(), "The event loop was blocked by the child")
            code, _, _ = await job
            self.assertEqual(code, 0)
        finally:
            if not job.done():
                job.cancel()
                await asyncio.gather(job, return_exceptions=True)

    async def test_timeout_and_cancellation_kill_and_reap_child(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                started = asyncio.Event()
                async def communicate():
                    started.set()
                    await asyncio.Future()
                child = SimpleNamespace(returncode=None, kill=Mock())
                first = True
                async def collect():
                    nonlocal first
                    if first:
                        first = False
                        return await communicate()
                    return b"", b""
                child.communicate = AsyncMock(side_effect=collect)
                with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=child)):
                    job = asyncio.create_task(FileCache._run_download_command(["unused"], timeout=0.02))
                    await started.wait()
                    if cancel:
                        job.cancel()
                    with self.assertRaises(asyncio.CancelledError if cancel else TimeoutError):
                        await job
                child.kill.assert_called_once()
                self.assertEqual(child.communicate.await_count, 2)

    async def test_failed_or_cancelled_command_cannot_publish_partial_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = FileCache(directory)
            target = Path(directory) / "result.mp4"
            for cancel in (False, True):
                async def run(command, **kwargs):
                    Path(command[1]).write_bytes(b"partial")
                    if cancel:
                        raise asyncio.CancelledError()
                    return 22, b"", b"signed URL must not be logged"
                with patch.object(cache, "_run_download_command", side_effect=run):
                    with self.assertRaises(asyncio.CancelledError if cancel else RuntimeError):
                        await cache._download_with_command(["curl", str(target)], target, env={})
                self.assertFalse(target.exists())
                self.assertEqual(list(Path(directory).iterdir()), [])

    async def test_successful_command_atomically_publishes_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = FileCache(directory)
            target = Path(directory) / "result.mp4"
            async def run(command, **kwargs):
                self.assertNotEqual(command[1], str(target))
                self.assertFalse(target.exists())
                Path(command[1]).write_bytes(b"complete")
                return 0, b"", b""
            with patch.object(cache, "_run_download_command", side_effect=run):
                await cache._download_with_command(["curl", str(target)], target, env={})
            self.assertEqual(target.read_bytes(), b"complete")
            self.assertEqual(list(Path(directory).iterdir()), [target])

    async def test_socks_fallback_uses_same_proxy_and_rejects_http_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = FileCache(directory)
            cache._resolve_download_proxy = AsyncMock(return_value="socks5://127.0.0.1:20035")
            session = SimpleNamespace(get=AsyncMock(side_effect=RuntimeError("transport failed")))
            manager = AsyncMock(); manager.__aenter__.return_value = session
            async def download(command, path, *, env):
                self.assertEqual(command[0], "curl")
                self.assertIn("--fail", command)
                self.assertEqual(command[command.index("-x") + 1], "socks5://127.0.0.1:20035")
                self.assertNotIn("ALL_PROXY", env)
                self.assertNotIn("NO_PROXY", env)
                path.write_bytes(b"complete")
            with patch("src.services.file_cache.AsyncSession", return_value=manager) as factory, \
                 patch.dict(os.environ, {"ALL_PROXY":"http://wrong:8080", "NO_PROXY":"*"}), \
                 patch.object(cache, "_download_with_command", side_effect=download) as fallback:
                result = await cache.download_and_cache("https://flow-content.google/video/test", "video")
            factory.assert_called_once_with(trust_env=False)
            self.assertEqual(fallback.await_count, 1)
            self.assertEqual((Path(directory) / result).read_bytes(), b"complete")
