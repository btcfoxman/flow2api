import json
import unittest

from src.api.admin import _filter_superseded_async_video_submit_logs


class AdminLogsTests(unittest.TestCase):
    def test_filter_hides_async_video_submit_log_when_result_log_exists(self):
        request_id = "gen-123"
        logs = [
            {
                "id": 2,
                "operation": "generate_video",
                "status_code": 200,
                "status_text": "video_submitted",
                "response_body": json.dumps({"performance": {"request_id": request_id}}),
            },
            {
                "id": 1,
                "operation": "generate_video_async_result",
                "status_code": 400,
                "status_text": "failed",
                "request_body": json.dumps({"request_id": request_id, "task_id": "task-1"}),
            },
        ]

        filtered = _filter_superseded_async_video_submit_logs(logs)

        self.assertEqual([log["id"] for log in filtered], [1])

    def test_filter_keeps_video_submit_log_without_result_log(self):
        logs = [
            {
                "id": 2,
                "operation": "generate_video",
                "status_code": 200,
                "status_text": "video_submitted",
                "response_body": json.dumps({"performance": {"request_id": "gen-123"}}),
            }
        ]

        filtered = _filter_superseded_async_video_submit_logs(logs)

        self.assertEqual([log["id"] for log in filtered], [2])


if __name__ == "__main__":
    unittest.main()
