import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.api import routes
from src.services.model_capabilities import model_transport_error, supports_flow_model
from src.services.generation_handler import MODEL_CONFIG


class ModelCapabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_only_rejects_unsupported_before_enqueue_or_selection(self):
        db = SimpleNamespace(get_active_tokens=AsyncMock(return_value=[SimpleNamespace(auth_mode="flow")]),
                             enqueue_async_task=AsyncMock())
        handler = SimpleNamespace(db=db)
        normalized = routes.NormalizedGenerationRequest(model="veo_3_1_t2v_fast_landscape", prompt="test", images=[])
        with patch("src.services.model_capabilities.config", SimpleNamespace(captcha_method="native_cdp")), \
             patch.object(routes, "generation_handler", handler):
            result = await routes._create_deferred_async_video_task(normalized)
        self.assertEqual(result["error"]["code"], "model_not_supported")
        db.enqueue_async_task.assert_not_awaited()

    async def test_mixed_protocol_accounts_do_not_globally_reject_legacy(self):
        db = SimpleNamespace(get_active_tokens=AsyncMock(return_value=[SimpleNamespace(auth_mode="flow"),
                                                                       SimpleNamespace(auth_mode="labs")]))
        with patch("src.services.model_capabilities.config", SimpleNamespace(captcha_method="native_cdp")):
            self.assertIsNone(await model_transport_error(db, MODEL_CONFIG["veo_3_1_t2v_fast_landscape"]))

    def test_360_and_720_reference_video_remain_supported(self):
        for model in ("abra_r2v_4s_360p", "abra_r2v_10s", "abra_r2v_4s_720p"):
            self.assertTrue(supports_flow_model(MODEL_CONFIG[model]))
        self.assertFalse(supports_flow_model(MODEL_CONFIG["gemini-3.0-pro-image-portrait-2k"]))
