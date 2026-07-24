import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import websockets

from src.core.config import config
from src.services.browser_captcha_native_cdp import (
    BrowserCaptchaService,
    CdpConnection,
    NativeCdpAccountBrowser,
    _parse_proxy_url,
)


class NativeCdpConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_routes_responses_and_session_events(self):
        async def handler(websocket):
            request = json.loads(await websocket.recv())
            await websocket.send(
                json.dumps({"id": request["id"], "result": {"value": "ok"}})
            )
            await asyncio.sleep(0.02)
            await websocket.send(
                json.dumps(
                    {
                        "method": "Page.loadEventFired",
                        "sessionId": "session-1",
                        "params": {"timestamp": 123},
                    }
                )
            )

        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            connection = CdpConnection(f"ws://127.0.0.1:{port}")
            await connection.connect()
            event_task = asyncio.create_task(
                connection.wait_event(
                    "Page.loadEventFired",
                    session_id="session-1",
                    timeout=1,
                )
            )

            response = await connection.send("Runtime.enable", timeout=1)
            event = await event_task

            self.assertEqual(response, {"value": "ok"})
            self.assertEqual(event["timestamp"], 123)
            await connection.close()


class _FakeDatabase:
    def __init__(self, token_proxy=None, global_proxy=None, global_enabled=False):
        self.token_proxy = token_proxy
        self.global_proxy = global_proxy
        self.global_enabled = global_enabled

    async def get_token(self, token_id):
        return SimpleNamespace(captcha_proxy_url=self.token_proxy, st="session-token")

    async def get_captcha_config(self):
        return SimpleNamespace(
            browser_proxy_enabled=self.global_enabled,
            browser_proxy_url=self.global_proxy,
        )

    async def get_active_tokens(self):
        return []


class NativeCdpProxyTests(unittest.IsolatedAsyncioTestCase):
    def test_parse_authenticated_proxy(self):
        self.assertEqual(
            _parse_proxy_url("http://user:pass@proxy.example:8080"),
            ("http", "proxy.example", 8080, "user", "pass"),
        )

    async def test_token_proxy_takes_priority(self):
        browser = NativeCdpAccountBrowser(
            7,
            _FakeDatabase(
                token_proxy="socks5://token.proxy:1080",
                global_proxy="http://global.proxy:8080",
                global_enabled=True,
            ),
        )

        binding = await browser._resolve_proxy()

        self.assertEqual(binding.source, "token")
        self.assertEqual(binding.url, "socks5://token.proxy:1080")

    async def test_global_proxy_is_required_fallback(self):
        browser = NativeCdpAccountBrowser(7, _FakeDatabase())

        with self.assertRaisesRegex(RuntimeError, "no token proxy"):
            await browser._resolve_proxy()


class _FakeAccountBrowser:
    instances = {}
    blocked_tokens = set()
    solve_started = {}
    solve_release = {}

    def __init__(self, token_id, db):
        self.token_id = int(token_id)
        self.db = db
        self.is_running = False
        self.busy_count = 0
        self.last_used_at = 0
        self.last_fingerprint = {"user_agent": f"token-{token_id}"}
        self.solve_calls = []
        self.profile_dir = SimpleNamespace(name=f"token-{token_id}")
        self.process = None
        self.proxy_binding = None
        self.solve_count = 0
        self.last_error = None
        type(self).instances[self.token_id] = self

    @property
    def is_busy(self):
        return self.busy_count > 0

    async def start(self):
        self.is_running = True

    async def solve(self, project_id, action, website_key):
        self.solve_calls.append((project_id, action, website_key))
        if self.token_id in type(self).blocked_tokens:
            type(self).solve_started.setdefault(self.token_id, asyncio.Event()).set()
            await type(self).solve_release.setdefault(self.token_id, asyncio.Event()).wait()
        return f"captcha-{self.token_id}"

    async def stop(self, reason):
        self.is_running = False

    async def delete_profile(self):
        self.is_running = False

    def status(self):
        return {"token_id": self.token_id, "running": self.is_running}


class NativeCdpServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_count = config.browser_count
        config.set_browser_count(3)
        BrowserCaptchaService._instance = None
        _FakeAccountBrowser.instances = {}
        _FakeAccountBrowser.blocked_tokens = set()
        _FakeAccountBrowser.solve_started = {}
        _FakeAccountBrowser.solve_release = {}

    async def asyncTearDown(self):
        config.set_browser_count(self.original_count)
        if BrowserCaptchaService._instance is not None:
            await BrowserCaptchaService._instance.close()

    async def test_one_worker_per_token_shared_by_projects(self):
        with patch(
            "src.services.browser_captcha_native_cdp.NativeCdpAccountBrowser",
            _FakeAccountBrowser,
        ):
            service = await BrowserCaptchaService.get_instance(_FakeDatabase())

            first, first_ref = await service.get_token("project-a", token_id=1)
            second, second_ref = await service.get_token("project-b", token_id=1)
            third, third_ref = await service.get_token("project-c", token_id=2)

            self.assertEqual(first, "captcha-1")
            self.assertEqual(second, "captcha-1")
            self.assertEqual(third, "captcha-2")
            self.assertEqual(first_ref, "native:1")
            self.assertEqual(second_ref, "native:1")
            self.assertEqual(third_ref, "native:2")
            self.assertEqual(len(_FakeAccountBrowser.instances), 2)
            self.assertEqual(
                [call[0] for call in _FakeAccountBrowser.instances[1].solve_calls],
                ["project-a", "project-b"],
            )

    async def test_busy_worker_is_not_evicted_while_next_account_waits(self):
        config.set_browser_count(1)
        _FakeAccountBrowser.blocked_tokens = {1}
        with patch(
            "src.services.browser_captcha_native_cdp.NativeCdpAccountBrowser",
            _FakeAccountBrowser,
        ):
            service = await BrowserCaptchaService.get_instance(_FakeDatabase())
            first_task = asyncio.create_task(
                service.get_token("project-a", token_id=1)
            )
            while 1 not in _FakeAccountBrowser.solve_started:
                await asyncio.sleep(0)
            await _FakeAccountBrowser.solve_started[1].wait()

            second_task = asyncio.create_task(
                service.get_token("project-b", token_id=2)
            )
            await asyncio.sleep(0.05)

            self.assertEqual(service.get_status()["queued"], 1)
            self.assertTrue(_FakeAccountBrowser.instances[1].is_running)
            self.assertFalse(_FakeAccountBrowser.instances[2].is_running)

            _FakeAccountBrowser.solve_release[1].set()
            first_result, second_result = await asyncio.gather(first_task, second_task)

            self.assertEqual(first_result[0], "captcha-1")
            self.assertEqual(second_result[0], "captcha-2")
            self.assertFalse(_FakeAccountBrowser.instances[1].is_running)
            self.assertTrue(_FakeAccountBrowser.instances[2].is_running)
