import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from src.api.routes import (
    IMAGE_LOAD_FAILURE_MESSAGE,
    _build_remote_media_headers,
    _download_remote_media_data,
    _load_image_bytes_from_uri,
)


class FakeResponse:
    def __init__(self, status_code, content=b"", content_type=""):
        self.status_code = status_code
        self.content = content
        self.headers = {"content-type": content_type} if content_type else {}


class FakeAsyncSession:
    def __init__(self, outcomes, calls):
        self.outcomes = outcomes
        self.calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def get(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class ReferenceMediaDownloadTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _file_cache(media_proxy=None, request_proxy=None):
        return SimpleNamespace(
            _resolve_download_proxy=AsyncMock(return_value=media_proxy),
            proxy_manager=SimpleNamespace(
                get_request_proxy_url=AsyncMock(return_value=request_proxy)
            ),
        )

    async def test_retryable_failures_rotate_media_request_and_direct_routes(self):
        outcomes = [
            RuntimeError("connection reset"),
            FakeResponse(502, b"bad gateway", "text/plain"),
            FakeResponse(200, b"\xff\xd8\xffimage", "image/jpeg"),
        ]
        calls = []
        file_cache = self._file_cache(
            media_proxy="http://media-proxy:8080",
            request_proxy="http://request-proxy:8080",
        )

        with (
            patch(
                "src.api.routes.AsyncSession",
                side_effect=lambda: FakeAsyncSession(outcomes, calls),
            ),
            patch("src.api.routes.asyncio.sleep", new=AsyncMock()) as sleep_mock,
        ):
            result = await _download_remote_media_data(
                "https://media.example/reference.jpg",
                "image",
                60,
                file_cache,
            )

        self.assertEqual(result, b"\xff\xd8\xffimage")
        self.assertEqual(
            [call["proxy"] for call in calls],
            [
                "http://media-proxy:8080",
                "http://request-proxy:8080",
                None,
            ],
        )
        self.assertTrue(all(call["allow_redirects"] for call in calls))
        self.assertEqual(sleep_mock.await_count, 2)

    async def test_non_retryable_client_error_stops_immediately(self):
        outcomes = [FakeResponse(404, b"not found", "text/plain")]
        calls = []
        file_cache = self._file_cache(media_proxy="http://media-proxy:8080")

        with (
            patch(
                "src.api.routes.AsyncSession",
                side_effect=lambda: FakeAsyncSession(outcomes, calls),
            ),
            patch("src.api.routes.asyncio.sleep", new=AsyncMock()) as sleep_mock,
        ):
            result = await _download_remote_media_data(
                "https://media.example/missing.jpg",
                "image",
                60,
                file_cache,
            )

        self.assertIsNone(result)
        self.assertEqual(len(calls), 1)
        sleep_mock.assert_not_awaited()

    async def test_html_success_response_is_rejected_and_retried(self):
        outcomes = [
            FakeResponse(200, b"<html>challenge</html>", "text/html"),
            FakeResponse(200, b"\x89PNG\r\n\x1a\nimage", "application/octet-stream"),
        ]
        calls = []

        with (
            patch(
                "src.api.routes.AsyncSession",
                side_effect=lambda: FakeAsyncSession(outcomes, calls),
            ),
            patch("src.api.routes.asyncio.sleep", new=AsyncMock()),
        ):
            result = await _download_remote_media_data(
                "https://media.example/reference.png",
                "image",
                60,
                None,
            )

        self.assertEqual(result, b"\x89PNG\r\n\x1a\nimage")
        self.assertEqual([call["proxy"] for call in calls], [None, None])

    async def test_public_failure_does_not_echo_material_url(self):
        material_url = "https://media.example/private/path.jpg?signature=secret"

        with patch(
            "src.api.routes.retrieve_image_data",
            new=AsyncMock(return_value=None),
        ):
            with self.assertRaises(HTTPException) as raised:
                await _load_image_bytes_from_uri(material_url)

        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(raised.exception.detail, IMAGE_LOAD_FAILURE_MESSAGE)
        self.assertNotIn(material_url, raised.exception.detail)
        self.assertNotIn("Flow", raised.exception.detail)

    def test_third_party_referer_uses_its_own_origin(self):
        headers = _build_remote_media_headers(
            "https://www.bananapro.top/api/media/cache%3Aexample.jpg",
            "image",
        )

        self.assertEqual(headers["Referer"], "https://www.bananapro.top/")

    def test_google_media_keeps_labs_referer(self):
        headers = _build_remote_media_headers(
            "https://example.googleusercontent.com/media/image.jpg",
            "image",
        )

        self.assertEqual(headers["Referer"], "https://labs.google/")

    def test_third_party_referer_does_not_expose_url_credentials(self):
        headers = _build_remote_media_headers(
            "https://user:password@media.example:8443/reference.jpg",
            "image",
        )

        self.assertEqual(headers["Referer"], "https://media.example:8443/")
        self.assertNotIn("password", headers["Referer"])


if __name__ == "__main__":
    unittest.main()
