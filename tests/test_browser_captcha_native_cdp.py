import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import websockets

from src.core.config import config
from src.services.browser_captcha_native_cdp import (
    BrowserCaptchaService,
    CdpConnection,
    NativeCdpAccountBrowser,
    _is_recaptcha_profile_risk_error,
    _parse_proxy_url,
    _proxy_egress_key,
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
    def __init__(
        self,
        token_proxy=None,
        global_proxy=None,
        global_enabled=False,
        token_proxies=None,
        logs=None,
    ):
        self.token_proxy = token_proxy
        self.global_proxy = global_proxy
        self.global_enabled = global_enabled
        self.token_proxies = token_proxies or {}
        self.logs = logs or []

    async def get_token(self, token_id):
        return SimpleNamespace(
            captcha_proxy_url=self.token_proxies.get(int(token_id), self.token_proxy),
            st="session-token",
        )

    async def get_captcha_config(self):
        return SimpleNamespace(
            browser_proxy_enabled=self.global_enabled,
            browser_proxy_url=self.global_proxy,
        )

    async def get_active_tokens(self):
        return []

    async def get_logs(self, limit=500, include_payload=False):
        return list(self.logs[:limit])


class NativeCdpProxyTests(unittest.IsolatedAsyncioTestCase):
    def test_identifies_recaptcha_profile_risk_error(self):
        self.assertTrue(
            _is_recaptcha_profile_risk_error(
                "PUBLIC_ERROR_UNUSUAL_ACTIVITY: reCAPTCHA evaluation failed"
            )
        )
        self.assertFalse(_is_recaptcha_profile_risk_error("HTTP Error 429"))

    def test_legacy_profile_is_reset_before_reuse(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "src.services.browser_captcha_native_cdp._profile_root",
            return_value=Path(temp_dir),
        ):
            (Path(temp_dir) / "token-7").mkdir()
            browser = NativeCdpAccountBrowser(7, _FakeDatabase())

        self.assertTrue(browser._profile_reset_pending)
        self.assertEqual(browser._profile_reset_reason, "profile_baseline_upgrade")

    def test_parse_authenticated_proxy(self):
        self.assertEqual(
            _parse_proxy_url("http://user:pass@proxy.example:8080"),
            ("http", "proxy.example", 8080, "user", "pass"),
        )

    def test_proxy_egress_key_normalizes_local_xray_aliases(self):
        localhost_key = _proxy_egress_key("http://user:one@127.0.0.1:18080")
        docker_host_key = _proxy_egress_key(
            "socks5://other:secret@host.docker.internal:18080"
        )
        other_port_key = _proxy_egress_key("http://127.0.0.1:18081")

        self.assertEqual(localhost_key, docker_host_key)
        self.assertNotEqual(localhost_key, other_port_key)

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
    fetch_errors = {}

    def __init__(self, token_id, db):
        self.token_id = int(token_id)
        self.db = db
        self.is_running = False
        self.busy_count = 0
        self.last_used_at = 0
        self.last_fingerprint = {"user_agent": f"token-{token_id}"}
        self.solve_calls = []
        self.fetch_calls = []
        self.profile_dir = SimpleNamespace(name=f"token-{token_id}")
        self.process = None
        self.proxy_binding = None
        self.solve_count = 0
        self.last_error = None
        self.video_submit_reservations = 0
        type(self).instances[self.token_id] = self

    @property
    def is_busy(self):
        return self.busy_count > 0 or self.video_submit_reservations > 0

    def reserve_for_video_submit(self):
        self.video_submit_reservations += 1

    def consume_video_submit_reservation(self):
        self.video_submit_reservations = max(0, self.video_submit_reservations - 1)

    async def start(self):
        self.is_running = True

    async def solve(self, project_id, action, website_key):
        self.solve_calls.append((project_id, action, website_key))
        if self.token_id in type(self).blocked_tokens:
            type(self).solve_started.setdefault(self.token_id, asyncio.Event()).set()
            await type(self).solve_release.setdefault(self.token_id, asyncio.Event()).wait()
        return f"captcha-{self.token_id}"

    async def fetch_json(self, **kwargs):
        self.fetch_calls.append(kwargs)
        error = type(self).fetch_errors.get(self.token_id)
        if error is not None:
            raise error
        return {"projectId": kwargs["project_id"], "ok": True}

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
        _FakeAccountBrowser.fetch_errors = {}

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

    async def test_browser_fetch_reuses_account_worker(self):
        with patch(
            "src.services.browser_captcha_native_cdp.NativeCdpAccountBrowser",
            _FakeAccountBrowser,
        ):
            service = await BrowserCaptchaService.get_instance(_FakeDatabase())

            await service.get_token("project-a", token_id=1)
            result = await service.fetch_json(
                token_id=1,
                project_id="project-a",
                url="https://example.test/video:submit",
                json_data={"clientContext": {"projectId": "project-a"}},
            )

            self.assertTrue(result["ok"])
            self.assertEqual(len(_FakeAccountBrowser.instances), 1)
            self.assertEqual(
                _FakeAccountBrowser.instances[1].fetch_calls[0]["project_id"],
                "project-a",
            )

    async def test_worker_fetch_runs_in_real_project_page_session(self):
        browser = NativeCdpAccountBrowser(7, _FakeDatabase())
        browser.start = AsyncMock()
        browser._get_or_create_project_session = AsyncMock(
            return_value=("target-1", "session-1")
        )
        browser._evaluate = AsyncMock(
            return_value={"status": 200, "statusText": "OK", "text": '{"ok": true}'}
        )

        result = await browser.fetch_json(
            project_id="project-a",
            url="https://example.test/video:submit",
            headers={"authorization": "Bearer test", "user-agent": "blocked"},
            json_data={"hello": "world"},
        )

        self.assertEqual(result, {"ok": True})
        browser._get_or_create_project_session.assert_awaited_once_with("project-a")
        expression = browser._evaluate.await_args.args[1]
        self.assertIn("credentials: 'include'", expression)
        self.assertNotIn('\\"user-agent\\"', expression)

    async def test_worker_marks_profile_for_reset_after_recaptcha_risk_rejection(self):
        browser = NativeCdpAccountBrowser(7, _FakeDatabase())
        browser._prepare_profile = AsyncMock()
        browser._get_or_create_project_session = AsyncMock(
            return_value=("target-1", "session-1")
        )
        browser._discard_project_session = AsyncMock()
        browser._evaluate = AsyncMock(
            return_value={
                "status": 429,
                "statusText": "Too Many Requests",
                "text": json.dumps(
                    {
                        "error": {
                            "message": "reCAPTCHA evaluation failed",
                            "details": [
                                {"reason": "PUBLIC_ERROR_UNUSUAL_ACTIVITY"}
                            ],
                        }
                    }
                ),
            }
        )

        with self.assertRaisesRegex(RuntimeError, "PUBLIC_ERROR_UNUSUAL_ACTIVITY"):
            await browser.fetch_json(
                project_id="project-a",
                url="https://example.test/video:submit",
                json_data={"hello": "world"},
            )

        self.assertTrue(browser._profile_reset_pending)
        browser._discard_project_session.assert_awaited_once_with("project-a")

    async def test_periodic_rotation_resets_profile_before_next_solve(self):
        browser = NativeCdpAccountBrowser(7, _FakeDatabase())
        browser.profile_solve_count = 10
        browser._reset_profile = AsyncMock()
        browser.start = AsyncMock()

        await browser._prepare_profile(for_solve=True)

        browser._reset_profile.assert_awaited_once_with(reason="solve_threshold_10")
        browser.start.assert_awaited_once_with()

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

    async def test_video_worker_is_reserved_until_bound_submit_starts(self):
        config.set_browser_count(1)
        with patch(
            "src.services.browser_captcha_native_cdp.NativeCdpAccountBrowser",
            _FakeAccountBrowser,
        ):
            service = await BrowserCaptchaService.get_instance(
                _FakeDatabase(token_proxy="http://127.0.0.1:18080")
            )
            token, _ = await service.get_token(
                "project-a",
                action="VIDEO_GENERATION",
                token_id=1,
            )
            self.assertEqual(token, "captcha-1")
            self.assertEqual(_FakeAccountBrowser.instances[1].video_submit_reservations, 1)

            next_account_task = asyncio.create_task(
                service.get_token("project-b", action="IMAGE_GENERATION", token_id=2)
            )
            await asyncio.sleep(0.05)

            self.assertEqual(service.get_status()["queued"], 1)
            self.assertTrue(_FakeAccountBrowser.instances[1].is_running)
            self.assertFalse(_FakeAccountBrowser.instances[2].is_running)

            await service.fetch_json(
                token_id=1,
                project_id="project-a",
                url="https://example.test/video:submit",
                json_data={},
                consume_video_reservation=True,
            )
            next_token, _ = await next_account_task

            self.assertEqual(next_token, "captcha-2")
            self.assertFalse(_FakeAccountBrowser.instances[1].is_running)
            self.assertTrue(_FakeAccountBrowser.instances[2].is_running)

    async def test_traffic_failure_quarantines_shared_proxy_but_not_other_exit(self):
        database = _FakeDatabase(
            token_proxies={
                1: "http://127.0.0.1:18080",
                2: "socks5://host.docker.internal:18080",
                3: "http://127.0.0.1:18081",
            }
        )
        _FakeAccountBrowser.fetch_errors[1] = RuntimeError("HTTP Error 429")
        with patch(
            "src.services.browser_captcha_native_cdp.NativeCdpAccountBrowser",
            _FakeAccountBrowser,
        ):
            service = await BrowserCaptchaService.get_instance(database)

            with self.assertRaisesRegex(RuntimeError, "429"):
                await service.fetch_json(
                    token_id=1,
                    project_id="project-a",
                    url="https://example.test/video:submit",
                    json_data={},
                    consume_video_reservation=True,
                )

            shared_state = await service.get_video_proxy_state(2)
            other_state = await service.get_video_proxy_state(3)

            self.assertFalse(shared_state["available"])
            self.assertEqual(shared_state["failure_streak"], 1)
            self.assertTrue(other_state["available"])
            status = service.get_status()
            self.assertNotIn(
                "proxy_key",
                status["video_proxy_risk_groups"][0],
            )

    async def test_recent_log_history_restores_shared_proxy_cooldown(self):
        now = datetime.now(tz=timezone.utc)
        database = _FakeDatabase(
            token_proxies={
                1: "http://127.0.0.1:18080",
                2: "http://host.docker.internal:18080",
            },
            logs=[
                {
                    "operation": "generate_video",
                    "token_id": 1,
                    "status_code": 429,
                    "updated_at": (now - timedelta(minutes=3)).isoformat(),
                },
                {
                    "operation": "generate_video",
                    "token_id": 2,
                    "status_code": 429,
                    "updated_at": (now - timedelta(seconds=10)).isoformat(),
                },
            ],
        )
        service = await BrowserCaptchaService.get_instance(database)
        active_tokens = [
            SimpleNamespace(id=1, captcha_proxy_url=database.token_proxies[1]),
            SimpleNamespace(id=2, captcha_proxy_url=database.token_proxies[2]),
        ]

        await service.hydrate_proxy_risk_history(active_tokens)
        state = await service.get_video_proxy_state(1)

        self.assertFalse(state["available"])
        self.assertEqual(state["failure_streak"], 2)
        self.assertGreater(state["cooldown_remaining_seconds"], 0)
        self.assertTrue(service.get_status()["proxy_risk_history_loaded"])
