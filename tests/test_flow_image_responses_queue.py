import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.api import routes
from src.core.database import Database


class FlowImageResponsesQueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db = Database(str(Path(self.directory.name) / "flow.db"))
        await self.db.init_db()
        self.handler_patch = patch.object(
            routes, "generation_handler", SimpleNamespace(db=self.db)
        )
        self.handler_patch.start()
        self.addCleanup(self.handler_patch.stop)
        self.capability_patch = patch(
            "src.services.model_capabilities.model_transport_error",
            new_callable=AsyncMock,
            return_value=None,
        )
        self.capability_patch.start()
        self.addCleanup(self.capability_patch.stop)
        app = FastAPI()
        app.include_router(routes.router)
        app.dependency_overrides[routes.verify_api_key_flexible] = lambda: "test-key"
        self.client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        self.addAsyncCleanup(self.client.aclose)
        self.addAsyncCleanup(routes.stop_image_response_queue)

    async def test_url_input_is_fetched_after_submission_and_result_survives_restart(self):
        image_url = "https://example.test/reference.png"
        result_url = "https://example.test/result.png"
        request = {
            "model": "gemini-3.1-flash-image-square",
            "background": True,
            "input": [{"role": "user", "content": [
                {"type": "input_text", "text": "a blue circle"},
                {"type": "input_image", "image_url": image_url},
            ]}],
            "tools": [{"type": "image_generation"}],
        }
        with patch.object(routes, "_load_image_bytes_from_uri", new_callable=AsyncMock,
                          return_value=b"small image") as download, \
             patch.object(routes, "_collect_non_stream_result", new_callable=AsyncMock,
                          return_value=json.dumps({"url": result_url})) as generate:
            submitted = await self.client.post("/v1/responses", json=request)
            self.assertEqual(submitted.status_code, 200, submitted.text)
            response_id = submitted.json()["id"]
            self.assertEqual(submitted.json()["status"], "queued")
            download.assert_not_awaited()
            generate.assert_not_awaited()

            await routes.start_image_response_queue()
            for _ in range(100):
                result = await self.client.get(f"/v1/responses/{response_id}")
                if result.json()["status"] == "completed":
                    break
                await asyncio.sleep(0.02)
            self.assertEqual(result.json()["status"], "completed", result.text)
            download.assert_awaited_once_with(image_url)
            self.assertEqual(result.json()["output"][0]["url"], result_url)
            self.assertIsNone(result.json()["output"][0]["result"])

            await routes.stop_image_response_queue()
            with patch.object(routes, "generation_handler",
                              SimpleNamespace(db=Database(self.db.db_path))):
                persisted = await self.client.get(f"/v1/responses/{response_id}")
            self.assertEqual(persisted.json()["output"][0]["url"], result_url)

    async def test_queue_capacity_and_url_only_contract(self):
        request = {
            "model": "gemini-3.1-flash-image-square",
            "background": True,
            "input": "a blue circle",
        }
        with patch.dict("os.environ", {"FLOW_IMAGE_RESPONSE_QUEUE_CAPACITY": "1"}):
            first = await self.client.post("/v1/responses", json=request)
            self.assertEqual(first.status_code, 200, first.text)
            second = await self.client.post("/v1/responses", json=request)
            self.assertEqual(second.status_code, 429)
        invalid = dict(request, response_format="b64_json")
        response = await self.client.post("/v1/responses", json=invalid)
        self.assertEqual(response.status_code, 400)
        invalid = dict(request, input=[{"type": "input_image", "image_url": "data:image/png;base64,AAA"}])
        response = await self.client.post("/v1/responses", json=invalid)
        self.assertEqual(response.status_code, 400)

    async def test_interrupted_generation_is_not_resubmitted(self):
        accepted = await self.db.enqueue_image_response_task(
            response_id="resp_interrupted", model="image-model",
            request_payload=json.dumps({"prompt": "test", "image_uris": []}), capacity=1,
        )
        self.assertTrue(accepted)
        claimed = await self.db.claim_next_image_response_task()
        self.assertEqual(claimed["status"], "in_progress")
        await routes.start_image_response_queue()
        task = await self.db.get_image_response_task("resp_interrupted")
        self.assertEqual(task["status"], "failed")
        self.assertIn("automatic resubmission stopped", task["error"])
