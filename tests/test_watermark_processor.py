import unittest

import httpx
from fastapi import FastAPI

from src.api import routes
from src.api.routes import _normalize_video_create_payload
from src.core.auth import verify_api_key_flexible
from src.core.models import Task
from src.services.watermark_processor import WatermarkProcessor


class FailingFileCache:
    async def download_and_cache(self, url, media_type):
        raise RuntimeError("download failed")


class RecordingWatermarkProcessor:
    def __init__(self, result_url=None, error=None):
        self.result_url = result_url
        self.error = error
        self.calls = []

    async def remove_watermark(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.result_url


class FakeGenerationHandler:
    def __init__(self, processor):
        self.watermark_processor = processor
        self.file_cache = object()


class WatermarkProcessorTests(unittest.TestCase):
    def test_watermark_request_accepts_legacy_video_url_alias(self):
        request = routes.WatermarkRemovalRequest.model_validate(
            {"video_url": "https://flow-content.google/video/example.mp4"}
        )

        self.assertEqual(
            request.source_url,
            "https://flow-content.google/video/example.mp4",
        )

    def test_task_default_watermark_is_false(self):
        task = Task(
            task_id="task-1",
            token_id=1,
            model="abra_r2v_10s",
            prompt="hello",
            status="processing",
        )

        self.assertFalse(task.watermark)

    def test_proxy_google_url_rewrites_flow_content_host(self):
        processor = WatermarkProcessor()

        rewritten = processor.proxy_google_url(
            "https://flow-content.google/video/example.mp4"
        )

        self.assertEqual(
            rewritten,
            "https://file-vercel-fl-go.aiid.edu.kg/video/example.mp4",
        )

    def test_proxy_google_url_leaves_other_hosts_unchanged(self):
        processor = WatermarkProcessor()

        rewritten = processor.proxy_google_url("https://example.com/video.mp4")

        self.assertEqual(rewritten, "https://example.com/video.mp4")


class WatermarkProcessorAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_video_create_payload_defaults_watermark_false(self):
        normalized = await _normalize_video_create_payload(
            {"model": "abra_r2v_10s", "prompt": "hello"}
        )

        self.assertFalse(normalized.watermark)

    async def test_video_create_payload_preserves_explicit_watermark_true(self):
        normalized = await _normalize_video_create_payload(
            {"model": "abra_r2v_10s", "prompt": "hello", "watermark": True}
        )

        self.assertTrue(normalized.watermark)

    async def test_watermark_false_falls_back_to_proxy_url_on_processing_failure(self):
        processor = WatermarkProcessor()

        rewritten = await processor.apply_policy(
            url="https://flow-content.google/video/example.mp4",
            watermark=False,
            file_cache=FailingFileCache(),
            public_base_url="https://api.example.com",
        )

        self.assertEqual(
            rewritten,
            "https://file-vercel-fl-go.aiid.edu.kg/video/example.mp4",
        )

    async def test_public_removal_rejects_non_flow_video_url(self):
        processor = WatermarkProcessor()

        with self.assertRaisesRegex(ValueError, "source_url host"):
            await processor.remove_watermark(
                url="https://example.com/video.mp4",
                file_cache=FailingFileCache(),
                public_base_url="https://api.example.com",
            )
        with self.assertRaisesRegex(ValueError, "valid HTTPS URL"):
            await processor.remove_watermark(
                url="https://flow-content.google:8443/video.mp4",
                file_cache=FailingFileCache(),
                public_base_url="https://api.example.com",
            )

    async def test_public_removal_accepts_aiid_root_and_subdomains(self):
        processor = WatermarkProcessor()
        accepted_urls = (
            "https://aiid.edu.kg/video.mp4",
            "https://cdn.aiid.edu.kg/video.mp4",
            "https://file-vercel-fl-go.aiid.edu.kg/video.mp4",
        )

        for url in accepted_urls:
            with self.subTest(url=url):
                self.assertEqual(processor.validate_source_url(url), url)

        with self.assertRaisesRegex(ValueError, "source_url host"):
            processor.validate_source_url(
                "https://aiid.edu.kg.example.com/video.mp4"
            )

    async def test_public_remove_watermark_endpoint_returns_uploaded_video_url(self):
        processor = RecordingWatermarkProcessor(
            result_url="https://cdn.example.com/watermark/video.gwt.mp4"
        )
        previous_handler = routes.generation_handler
        routes.set_generation_handler(FakeGenerationHandler(processor))
        app = FastAPI()
        app.include_router(routes.router)
        app.dependency_overrides[verify_api_key_flexible] = lambda: "test-key"

        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://api.example.com",
            ) as client:
                response = await client.post(
                    "/v1/videos/remove-watermark",
                    json={
                        "source_url": "https://flow-content.google/video/example.mp4"
                    },
                )
        finally:
            routes.set_generation_handler(previous_handler)

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["id"].startswith("wmr_"))
        self.assertEqual(payload["object"], "video.watermark_removal")
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(
            payload["video_url"],
            "https://cdn.example.com/watermark/video.gwt.mp4",
        )
        self.assertEqual(len(processor.calls), 1)
        self.assertEqual(
            processor.calls[0]["url"],
            "https://flow-content.google/video/example.mp4",
        )

    async def test_public_remove_watermark_endpoint_reports_missing_configuration(self):
        processor = RecordingWatermarkProcessor(
            error=RuntimeError("S3 upload is disabled")
        )
        previous_handler = routes.generation_handler
        routes.set_generation_handler(FakeGenerationHandler(processor))
        app = FastAPI()
        app.include_router(routes.router)
        app.dependency_overrides[verify_api_key_flexible] = lambda: "test-key"

        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://api.example.com",
            ) as client:
                response = await client.post(
                    "/v1/videos/remove-watermark",
                    json={
                        "source_url": "https://flow-content.google/video/example.mp4"
                    },
                )
        finally:
            routes.set_generation_handler(previous_handler)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json()["detail"],
            "Watermark removal service is not configured",
        )


if __name__ == "__main__":
    unittest.main()
