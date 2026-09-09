import importlib.util
from pathlib import Path
import unittest

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
