import unittest

from src.api.routes import _normalize_video_create_payload
from src.core.models import Task
from src.services.watermark_processor import WatermarkProcessor


class FailingFileCache:
    async def download_and_cache(self, url, media_type):
        raise RuntimeError("download failed")


class WatermarkProcessorTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
