import importlib.util
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.core.monitoring import render_main_metrics

spec = importlib.util.spec_from_file_location("pre_deploy_guard", Path(__file__).parents[1] / "tools/check_pre_deploy.py")
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


class DeployGuardTests(unittest.TestCase):
    def test_zero_metrics_pass(self):
        guard.check_metrics("flow2api_image_inflight_total 0.0\nflow2api_video_inflight_total 0.0\n")

    def test_active_or_missing_metrics_fail_closed(self):
        for data in ("", "flow2api_image_inflight_total 0.0",
                     "flow2api_image_inflight_total 0.0\nflow2api_video_inflight_total 1.0"):
            with self.assertRaises(RuntimeError):
                guard.check_metrics(data)

    def test_integrated_login_blocks_restart_but_old_versions_can_upgrade(self):
        base = "flow2api_image_inflight_total 0\nflow2api_video_inflight_total 0\n"
        guard.check_metrics(base)
        guard.check_metrics(base + "flow2api_account_login_active 0\n")
        for value in ('1', '-1', 'NaN'):
            with self.subTest(value=value), self.assertRaisesRegex(RuntimeError, 'login'):
                guard.check_metrics(base + f"flow2api_account_login_active {value}\n")


class LoginMetricTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_lease_phase_blocks_deploy_and_cleanup_clears_it(self):
        manager = SimpleNamespace(active=None)
        with patch('src.core.monitoring.update_main_runtime_metrics', AsyncMock()):
            for phase in ('starting', 'login', 'verifying', 'closing', None):
                manager.active = {'phase': phase, 'ticket': 'secret-viewer'} if phase else None
                payload = (await render_main_metrics(None, account_login_manager=manager)).decode()
                self.assertIn('flow2api_account_login_active ' + ('1.0' if phase else '0.0'), payload)
                self.assertNotIn('secret-viewer', payload)
