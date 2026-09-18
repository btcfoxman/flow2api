import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from src.api.admin import (
    CaptchaScoreTestRequest,
    _filter_superseded_async_video_submit_logs,
    test_captcha_score as run_captcha_score_test,
    get_logs, get_log_detail,
)


class AdminLogsTests(unittest.TestCase):
    def test_retry_attempts_are_superseded_by_same_queue_job_not_request_id(self):
        logs = [
            {"id": 3, "operation": "generate_video", "status_text": "retrying",
             "status_code": 503, "request_body": json.dumps({"queue_task_id": "public-1", "request_id": "attempt-1"})},
            {"id": 2, "operation": "generate_video_async_result", "status_text": "completed",
             "status_code": 200, "request_body": json.dumps({"queue_task_id": "public-1", "request_id": "attempt-2"})},
            {"id": 1, "operation": "generate_video", "status_text": "retrying",
             "status_code": 503, "request_body": json.dumps({"queue_task_id": "public-2"})},
        ]
        self.assertEqual([row["id"] for row in _filter_superseded_async_video_submit_logs(logs)], [2, 1])
        self.assertEqual(logs[0]["status_text"], "retrying")

    def test_unrelated_or_malformed_retry_records_are_not_hidden(self):
        for body in ('invalid json', 'null', '[]', '{}', '{"queue_task_id": []}'):
            with self.subTest(body=body):
                logs = [{"id": 1, "operation": "generate_video", "status_text": "retrying", "request_body": body},
                        {"id": 2, "operation": "generate_video_async_result", "request_body": '{}'}]
                self.assertEqual(_filter_superseded_async_video_submit_logs(logs), logs)

    @unittest.skipUnless(shutil.which("node"), "Node is required to execute log formatters")
    def test_browser_formatters_mark_retries_as_nonterminal(self):
        source = (Path(__file__).resolve().parents[1] / 'static/manage.html').read_text(encoding='utf-8')
        names = ('formatLogStatus', 'formatLogStatusClass', 'formatLogProgress',
                 'getLogOperationLabel', 'formatLogOutcome', 'formatLogOutcomeClass')
        expressions = [next(line.strip().removesuffix(',') for line in source.splitlines()
                            if line.strip().startswith(name + '=')) for name in names]
        harness = r'''
const assert = require('node:assert/strict');
const source = require('node:fs').readFileSync(0, 'utf8');
// Parse every inline script, including the long table/detail renderers.
for (const match of source.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)) new Function(match[1]);
const extractLogErrorSummary = row => row.error_summary || '';
const truncateLogText = value => value;
EXPRESSIONS
const retry = {status_text:'retrying', status_code:503, progress:100, error_summary:'busy'};
assert.equal(formatLogStatus(retry), '提交重试记录');
assert.equal(formatLogProgress(retry), '0%');
assert.match(formatLogOutcome(retry), /非最终生成结果/);
assert.match(formatLogStatusClass(retry), /amber/);
assert.match(formatLogOutcomeClass(retry), /amber/);
assert.equal(formatLogProgress({status_text:'completed',progress:100}), '100%');
assert.equal(formatLogStatus({status_text:'failed',status_code:503}), '失败');
assert.match(formatLogOutcomeClass({status_text:'failed',status_code:503}), /red/);
'''.replace('EXPRESSIONS', '\n'.join(expressions))
        subprocess.run([shutil.which('node'), '-e', harness], input=source, capture_output=True,
                       text=True, encoding='utf-8', timeout=15, check=True)

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


class AdminCaptchaScoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_retry_progress_is_normalized_without_rewriting_raw_evidence(self):
        raw = json.dumps({"error": "original attempt error", "performance": {"status": "failed"}})
        retry = {"id": 1, "operation": "generate_video", "status_text": "retrying", "status_code": 503,
                 "progress": 100, "response_body": raw, "response_body_excerpt": raw}
        fake_db = SimpleNamespace(get_logs=AsyncMock(return_value=[retry]),
                                  get_log_detail=AsyncMock(return_value=retry))
        with patch('src.api.admin.db', fake_db):
            rows = await get_logs(token='admin')
            detail = await get_log_detail(1, token='admin')
        self.assertEqual(rows[0]['progress'], 0)
        self.assertEqual(detail['progress'], 0)
        self.assertEqual(detail['status_code'], 503)
        self.assertEqual(detail['response_body'], raw)
        self.assertEqual(retry['progress'], 100)

    async def test_score_test_uses_request_body_object(self):
        fake_db = SimpleNamespace(
            get_captcha_config=AsyncMock(
                return_value=SimpleNamespace(
                    captcha_method="unsupported",
                    browser_proxy_enabled=False,
                    browser_proxy_url="",
                )
            )
        )

        with patch("src.api.admin.db", fake_db):
            result = await run_captcha_score_test(
                CaptchaScoreTestRequest(action="VIDEO_GENERATION"),
                _token="admin-session",
            )

        self.assertFalse(result["success"])
        self.assertEqual(result["action"], "VIDEO_GENERATION")
        self.assertIn("不支持", result["message"])


if __name__ == "__main__":
    unittest.main()
