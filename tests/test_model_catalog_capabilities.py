import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.api import routes
from src.services.generation_handler import MODEL_CONFIG
from src.services.model_capabilities import (
    log_capability_rejection, model_transport_error, supports_flow_model,
    uses_flow_only_protocol,
)


class ModelCatalogCapabilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = SimpleNamespace(get_active_tokens=AsyncMock(return_value=[
            SimpleNamespace(auth_mode="flow", credits=0, session_status="refresh_required")
        ]))
        self.handler_patch = patch.object(routes, "generation_handler", SimpleNamespace(db=self.db))
        self.config_patch = patch("src.services.model_capabilities.config",
                                  SimpleNamespace(captcha_method="native_cdp"))
        self.handler_patch.start()
        self.config_patch.start()
        self.addCleanup(self.handler_patch.stop)
        self.addCleanup(self.config_patch.stop)
        app = FastAPI()
        app.include_router(routes.router)
        app.dependency_overrides[routes.verify_api_key_flexible] = lambda: "test-key"
        self.client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        self.addAsyncCleanup(self.client.aclose)

    async def test_flow_discovery_agrees_across_all_model_endpoints(self):
        expected = {name for name, cfg in MODEL_CONFIG.items() if supports_flow_model(cfg)}
        response = await self.client.get("/v1/models")
        self.assertEqual(response.status_code, 200)
        self.assertEqual({m["id"] for m in response.json()["data"]}, expected)
        alias_response = await self.client.get("/v1/models/aliases")
        aliases = {m["id"] for m in alias_response.json()["data"]}
        self.assertIn("gemini-3.0-pro-image", aliases)
        self.assertIn("gemini-3.1-flash-image", aliases)
        self.assertNotIn("imagen-4.0-generate-preview", aliases)
        self.assertNotIn("sizes: 2k", json.dumps(alias_response.json()))
        for path in ("/v1beta/models", "/models"):
            response = await self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertEqual({m["name"].removeprefix("models/") for m in response.json()["models"]},
                             expected | aliases)
        # One protocol snapshot per endpoint, not one DB read per model.
        self.assertEqual(self.db.get_active_tokens.await_count, 4)
        for model in ("abra_r2v_4s_360p", "abra_r2v_10s", "abra_r2v_4s_720p",
                      "abra_t2v_4s_360p", "abra_t2v_10s", "abra_t2v_4s_720p"):
            self.assertIn(model, expected)

    async def test_filtered_gemini_model_cannot_be_discovered_individually(self):
        for path in ("/v1beta/models/", "/models/"):
            rejected = await self.client.get(path + "gemini-3.0-pro-image-landscape-2k")
            self.assertEqual(rejected.status_code, 404)
            accepted = await self.client.get(path + "gemini-3.0-pro-image")
            self.assertEqual(accepted.status_code, 200)
            self.assertNotIn("sizes: 2k", accepted.text)

    async def test_mixed_or_empty_accounts_preserve_catalog_and_update_without_restart(self):
        for accounts in ([], [SimpleNamespace(auth_mode="flow"), SimpleNamespace(auth_mode="labs")],
                         [SimpleNamespace(auth_mode="labs")], [SimpleNamespace()]):
            self.db.get_active_tokens.return_value = accounts
            response = await self.client.get("/v1/models")
            self.assertEqual({m["id"] for m in response.json()["data"]}, set(MODEL_CONFIG))
            self.assertIsNone(await model_transport_error(self.db,
                              MODEL_CONFIG["gemini-3.0-pro-image-landscape-2k"]))
        self.db.get_active_tokens.return_value = [SimpleNamespace(auth_mode="flow")]
        response = await self.client.get("/v1/models")
        self.assertNotIn("gemini-3.0-pro-image-landscape-2k", {m["id"] for m in response.json()["data"]})

    async def test_non_native_mode_does_not_query_account_health(self):
        with patch("src.services.model_capabilities.config", SimpleNamespace(captcha_method="remote")):
            self.assertFalse(await uses_flow_only_protocol(self.db))
            response = await self.client.get("/v1/models")
            self.assertEqual({m["id"] for m in response.json()["data"]}, set(MODEL_CONFIG))
        self.db.get_active_tokens.assert_not_awaited()

    async def test_responses_rejection_logs_canonical_model_without_creating_task(self):
        payload = {"model": "gemini-3.1-flash-image", "input": "private prompt must not be logged",
                   "generationConfig": {"imageConfig": {"imageSize": "2K"}}}
        with patch.object(routes, "_create_async_image_response_task", new_callable=AsyncMock) as create, \
             patch("src.services.model_capabilities.debug_logger.log_runtime_event") as logged:
            response = await self.client.post("/v1/responses", json=payload)
        self.assertEqual(response.status_code, 501)
        self.assertEqual(response.json()["error"]["code"], "model_not_supported")
        create.assert_not_awaited()
        logged.assert_called_once_with("generation_capability_rejected", stage="image_response_entry",
            reason="model_transport_unavailable", status_code=501, model="gemini-3.1-flash-image-landscape-2k")
        self.assertNotIn("private prompt", str(logged.call_args))

    async def test_responses_supported_model_still_creates_async_task(self):
        with patch.object(routes, "_create_async_image_response_task", new_callable=AsyncMock,
                          return_value={"id": "test-image", "status": "queued"}) as create, \
             patch("src.services.model_capabilities.debug_logger.log_runtime_event") as logged:
            response = await self.client.post("/v1/responses", json={
                "model": "gemini-3.1-flash-image", "input": "safe test"})
        self.assertEqual(response.status_code, 200)
        create.assert_awaited_once()
        self.assertEqual(create.await_args.kwargs["normalized"].model, "gemini-3.1-flash-image-landscape")
        logged.assert_not_called()

    async def test_video_rejection_uses_same_safe_diagnostic_and_never_enqueues(self):
        self.db.enqueue_async_task = AsyncMock()
        normalized = routes.NormalizedGenerationRequest(
            model="veo_3_1_t2v_fast_landscape", prompt="private prompt", images=[])
        with patch("src.services.model_capabilities.debug_logger.log_runtime_event") as logged:
            result = await routes._create_deferred_async_video_task(normalized)
        self.assertEqual(result["error"]["status_code"], 501)
        self.db.enqueue_async_task.assert_not_awaited()
        self.assertEqual(logged.call_args.kwargs["stage"], "video_queue_entry")
        self.assertNotIn("private prompt", str(logged.call_args))

    async def test_supported_text_video_enqueues_even_when_accounts_need_refresh(self):
        self.db.enqueue_async_task = AsyncMock(return_value={
            "position": 1, "capacity": 50, "expires_at": 2000000000})
        for model in ("abra_t2v_4s_360p", "abra_t2v_10s_720p"):
            normalized = routes.NormalizedGenerationRequest(model=model, prompt="test", images=[])
            with patch.object(routes, "_notify_async_task_queue"):
                result = await routes._create_deferred_async_video_task(normalized)
            self.assertEqual(result["status"], "queued")
            self.assertEqual(self.db.enqueue_async_task.await_args.kwargs["model"], model)

    def test_diagnostic_does_not_echo_unknown_model_input(self):
        with patch("src.services.model_capabilities.debug_logger.log_runtime_event") as logged:
            log_capability_rejection("private credential", MODEL_CONFIG, stage="generation_entry")
        self.assertEqual(logged.call_args.kwargs["model"], "unsupported")
        self.assertNotIn("private credential", str(logged.call_args))


if __name__ == "__main__":
    unittest.main()
