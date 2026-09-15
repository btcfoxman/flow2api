"""Shutdown must preserve profiles and reap owned children even during failure."""
import asyncio
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import yaml

from src.services.browser_captcha_native_cdp import NativeCdpAccountBrowser

MODULE = "src.services.browser_captcha_native_cdp"


class BrowserCleanupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch(MODULE + "._profile_root", return_value=self.directory))
        self.worker = NativeCdpAccountBrowser(7, SimpleNamespace())
        self.worker.profile_dir.mkdir(parents=True, exist_ok=True)
        self.cookie = self.worker.profile_dir / "Cookies"
        self.cookie.write_bytes(b"preserve-session")
        self.process = Mock()
        self.process.poll.return_value = None
        self.process.wait.return_value = 0
        self.connection = SimpleNamespace(send=AsyncMock(), close=AsyncMock())

    def prepare_start(self):
        self.worker._resolve_proxy = AsyncMock(return_value=SimpleNamespace(
            url="http://127.0.0.1:18080", signature="proxy", source="token"))
        self.stack.enter_context(patch(MODULE + ".local_session_state", return_value=None))
        self.stack.enter_context(patch(MODULE + "._detect_browser_executable", return_value="chromium"))
        self.stack.enter_context(patch(MODULE + ".subprocess.Popen", return_value=self.process))

    def assert_cleaned(self):
        self.assertIsNone(self.worker.process)
        self.assertIsNone(self.worker.connection)
        self.assertIsNone(self.worker._stop_task)
        self.assertEqual(self.cookie.read_bytes(), b"preserve-session")
        self.process.wait.assert_called_once_with(timeout=8)

    async def test_cancel_during_startup_waits_for_owned_process(self):
        self.prepare_start()
        started = asyncio.Event()

        async def wait_endpoint():
            started.set()
            await asyncio.Event().wait()

        self.worker._wait_for_devtools_endpoint = wait_endpoint
        task = asyncio.create_task(self.worker.start())
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assert_cleaned()

    async def test_partial_connection_failure_closes_socket_and_process(self):
        self.prepare_start()
        self.worker._wait_for_devtools_endpoint = AsyncMock(return_value="ws://127.0.0.1/test")
        self.connection.connect = AsyncMock(side_effect=RuntimeError("connect failed"))
        with patch(MODULE + ".CdpConnection", return_value=self.connection):
            with self.assertRaisesRegex(RuntimeError, "connect failed"):
                await self.worker.start()
        self.connection.close.assert_awaited_once()
        self.assert_cleaned()

    async def test_socket_close_failure_still_reaps_and_preserves_profile(self):
        self.worker.process = self.process
        self.worker.connection = self.connection
        self.connection.send.side_effect = RuntimeError("already disconnected")
        self.connection.close.side_effect = RuntimeError("socket close failed")
        extension = self.worker.profile_dir / "temporary-proxy-extension"
        extension.mkdir()
        self.worker.proxy_extension_dir = extension
        await self.worker.stop(reason="test")
        self.assert_cleaned()
        self.assertFalse(extension.exists())

    async def test_concurrent_stop_and_cancel_wait_for_single_cleanup(self):
        self.worker.process = self.process
        self.worker.connection = self.connection
        closing, release = asyncio.Event(), asyncio.Event()

        async def close():
            closing.set()
            await release.wait()

        self.connection.close.side_effect = close
        first = asyncio.create_task(self.worker.stop(reason="first"))
        await asyncio.wait_for(closing.wait(), 1)
        second = asyncio.create_task(self.worker.stop(reason="second"))
        await asyncio.sleep(0)
        first.cancel()
        await asyncio.sleep(0)
        self.assertFalse(first.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await first
        await second
        self.connection.close.assert_awaited_once()
        self.assert_cleaned()

    async def test_unresponsive_browser_escalates_then_waits(self):
        self.worker.process = self.process
        self.process.wait.side_effect = [subprocess.TimeoutExpired("chromium", 8),
                                         subprocess.TimeoutExpired("chromium", 5), 0]
        await self.worker.stop(reason="test_timeout")
        self.process.terminate.assert_called_once()
        self.process.kill.assert_called_once()
        self.assertEqual([c.kwargs["timeout"] for c in self.process.wait.call_args_list], [8, 5, 3])
        self.assertEqual(self.cookie.read_bytes(), b"preserve-session")


class DeploymentReaperTests(unittest.TestCase):
    def test_compose_templates_enable_init(self):
        root = Path(__file__).resolve().parents[1]
        for name, service in [("docker-compose.yml", "flow2api"),
                              ("docker-compose.headed.yml", "flow2api-headed")]:
            config = yaml.safe_load((root / name).read_text(encoding="utf-8"))
            self.assertIs(config["services"][service]["init"], True)
            self.assertEqual(config["services"][service]["stop_grace_period"], "120s")

    def test_pre_generated_override_and_verification_require_init(self):
        root = Path(__file__).resolve().parents[1]
        workflow = yaml.safe_load((root / ".github/workflows/deploy-pre.yml").read_text(encoding="utf-8"))
        steps = workflow["jobs"]["deploy"]["steps"]
        deploy = next(s["run"] for s in steps if s["name"] == "Pull and restart service")
        override = deploy.split("<<'YAML'\n", 1)[1].split("\nYAML", 1)[0]
        service = yaml.safe_load(override)["services"]["flow2api"]
        self.assertIs(service["init"], True)
        self.assertEqual(service["stop_grace_period"], "120s")
        verify = next(s["run"] for s in steps if s["name"] == "Verify container status")
        self.assertIn(".HostConfig.Init", verify)


if __name__ == "__main__":
    unittest.main()
