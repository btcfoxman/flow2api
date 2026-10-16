import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.api import admin
from src.core.database import Database


class AdminTaskLogTests(unittest.TestCase):
    def test_public_task_rollup_preserves_429_history_and_hides_upstream_duplicate(self):
        async def scenario(path):
            db = Database(str(path))
            await db.init_db()
            async with db._connect(write=True) as connection:
                await connection.execute(
                    "INSERT INTO tokens (id, st, email, name) VALUES (1, 'test-session', 'test@example.com', 'Test')"
                )
                await connection.execute(
                    """INSERT INTO tasks (task_id, token_id, model, prompt, status, progress)
                       VALUES ('upstream-1', 1, 'veo_3_1', 'private prompt', 'completed', 100)"""
                )
                await connection.execute(
                    """INSERT INTO tasks (task_id, token_id, model, prompt, status, progress, operations, result_urls)
                       VALUES (?, 1, 'veo_3_1', 'private prompt', 'completed', 100, ?, ?)""",
                    (
                        'flow2api-submit-public-1',
                        json.dumps([{'operation': {'name': 'upstream-1'}}]),
                        json.dumps(['https://example.com/result.mp4']),
                    ),
                )
                await connection.execute(
                    """INSERT INTO async_task_queue
                       (task_id, task_type, model, prompt, request_payload, status, attempt_count, last_error)
                       VALUES ('flow2api-submit-failed', 'video', 'veo_3_1', 'private prompt', '{}', 'failed', 2, 'upstream busy')"""
                )
                await connection.execute(
                    """INSERT INTO request_logs
                       (token_id, operation, request_body, response_body, status_code, duration, status_text)
                       VALUES (1, 'generate_video', ?, ?, 429, 1.5, 'retrying')""",
                    (
                        json.dumps({'queue_task_id': 'flow2api-submit-public-1', 'prompt': 'private prompt'}),
                        json.dumps({'error_message': 'unusual activity', 'rpc_diagnostic': {'grpc_code': 7, 'public_error': 'PUBLIC_ERROR_UNUSUAL_ACTIVITY'}}),
                    ),
                )
                await connection.execute(
                    """INSERT INTO request_logs
                       (token_id, operation, request_body, response_body, status_code, duration, status_text)
                       VALUES (1, 'generate_video', ?, '{}', 200, 2.0, 'video_submitted')""",
                    (json.dumps({'queue_task_id': 'flow2api-submit-public-1'}),),
                )
                await connection.commit()

            listed = await db.list_admin_tasks()
            listed_ids = {row['task_id'] for row in listed}
            self.assertEqual(listed_ids, {'flow2api-submit-public-1', 'flow2api-submit-failed'})
            self.assertNotIn('private prompt', json.dumps(listed))
            self.assertEqual((await db.get_admin_task_record('flow2api-submit-public-1'))['upstream_task_id'], 'upstream-1')

            with patch.object(admin, 'db', db):
                detail = await admin.get_admin_task_detail('flow2api-submit-public-1', token='admin')
            self.assertEqual(detail['status'], 'completed')
            self.assertEqual(detail['retry_count'], 1)
            self.assertEqual(detail['attempt_count'], 2)
            self.assertEqual([item['status_code'] for item in detail['attempts']], [429, 200])
            self.assertEqual(detail['attempts'][0]['public_error'], 'PUBLIC_ERROR_UNUSUAL_ACTIVITY')
            self.assertNotIn('private prompt', json.dumps(detail))

        with tempfile.TemporaryDirectory() as temp_dir:
            asyncio.run(scenario(Path(temp_dir) / 'flow.db'))


if __name__ == '__main__':
    unittest.main()
