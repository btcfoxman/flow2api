"""Video URL post-processing for Flow2API async video tasks."""

import asyncio
import os
from pathlib import Path
from urllib.parse import urlparse

from ..core.config import config
from ..core.logger import debug_logger
from .s3_uploader import S3Uploader


FLOW_GOOGLE_ORIGIN = "https://flow-content.google"
FLOW_GOOGLE_PROXY_ORIGIN = "https://file-vercel-fl-go.aiid.edu.kg"
AIID_ROOT_DOMAIN = "aiid.edu.kg"


class WatermarkProcessor:
    """Apply the caller's watermark policy to completed Google video URLs."""

    def __init__(self) -> None:
        self._semaphore = self._build_semaphore()
        self._s3_uploader = S3Uploader()

    def _build_semaphore(self) -> asyncio.Semaphore:
        concurrency = getattr(config, "gwt_video_concurrency", 1)
        try:
            concurrency = max(1, int(concurrency))
        except Exception:
            concurrency = 1
        return asyncio.Semaphore(concurrency)

    def refresh_runtime_config(self) -> None:
        self._semaphore = self._build_semaphore()
        self._s3_uploader = S3Uploader()

    def proxy_google_url(self, url: str) -> str:
        if not isinstance(url, str):
            return url
        proxy_origin = getattr(config, "flow_content_proxy_base", "") or FLOW_GOOGLE_PROXY_ORIGIN
        return url.replace(FLOW_GOOGLE_ORIGIN, proxy_origin.rstrip("/"))

    def validate_source_url(self, url: str) -> str:
        normalized = str(url or "").strip()
        parsed = urlparse(normalized)
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("source_url must be a valid HTTPS URL") from exc
        if (
            parsed.scheme.lower() != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or port not in {None, 443}
        ):
            raise ValueError("source_url must be a valid HTTPS URL")

        proxy_origin = getattr(config, "flow_content_proxy_base", "") or FLOW_GOOGLE_PROXY_ORIGIN
        allowed_hosts = {
            urlparse(FLOW_GOOGLE_ORIGIN).hostname,
            urlparse(proxy_origin).hostname,
        }
        hostname = parsed.hostname.lower()
        is_aiid_host = hostname == AIID_ROOT_DOMAIN or hostname.endswith(
            f".{AIID_ROOT_DOMAIN}"
        )
        if (
            hostname not in {str(host).lower() for host in allowed_hosts if host}
            and not is_aiid_host
        ):
            raise ValueError(
                "source_url host must be flow-content.google, aiid.edu.kg, "
                "an aiid.edu.kg subdomain, or the configured Flow content proxy"
            )
        return normalized

    async def remove_watermark(
        self,
        *,
        url: str,
        file_cache,
        public_base_url: str,
    ) -> str:
        return await self._remove_video_watermark(
            url=self.validate_source_url(url),
            file_cache=file_cache,
            public_base_url=public_base_url,
        )

    async def apply_policy(
        self,
        *,
        url: str,
        watermark: bool,
        file_cache,
        public_base_url: str,
    ) -> str:
        if not url:
            return url
        if watermark:
            return self.proxy_google_url(url)
        if FLOW_GOOGLE_ORIGIN not in url:
            return url
        try:
            return await self._remove_video_watermark(
                url=url,
                file_cache=file_cache,
                public_base_url=public_base_url,
            )
        except Exception as exc:
            debug_logger.log_warning(
                f"[GWT] watermark removal/upload failed, falling back to proxy URL: {exc}"
            )
            return self.proxy_google_url(url)

    async def _remove_video_watermark(
        self,
        *,
        url: str,
        file_cache,
        public_base_url: str,
    ) -> str:
        self._s3_uploader.ensure_available()
        download_url = self.proxy_google_url(url)
        try:
            input_filename = await file_cache.download_and_cache(download_url, "video")
        except Exception:
            if download_url == url:
                raise
            debug_logger.log_warning(
                "[GWT] proxy download failed, retrying original flow-content URL"
            )
            input_filename = await file_cache.download_and_cache(url, "video")
        input_path = Path(file_cache.cache_dir) / input_filename
        output_path = self._build_output_path(input_path)

        if output_path.exists() and output_path.stat().st_size > 0:
            return await self._upload_processed_video(output_path)

        async with self._semaphore:
            if output_path.exists() and output_path.stat().st_size > 0:
                return await self._upload_processed_video(output_path)
            await self._run_gwt_video(input_path=input_path, output_path=output_path)

        return await self._upload_processed_video(output_path)

    def _build_output_path(self, input_path: Path) -> Path:
        suffix = input_path.suffix or ".mp4"
        return input_path.with_name(f"{input_path.stem}.gwt{suffix}")

    async def _run_gwt_video(self, *, input_path: Path, output_path: Path) -> None:
        command = (
            os.getenv("FLOW2API_GWT_VIDEO_BIN")
            or getattr(config, "gwt_video_command", "")
            or "gwt-video"
        )
        args = [
            command,
            "--no-banner",
            "--quiet",
            "--veo",
            "--force",
            "-i",
            str(input_path),
            "-o",
            str(output_path),
        ]
        debug_logger.log_info(f"[GWT] queue processing: {input_path.name}")
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await process.communicate()
        except FileNotFoundError as exc:
            raise RuntimeError(f"gwt-video not found: {command}") from exc

        if process.returncode != 0:
            message = (stderr or stdout or b"").decode("utf-8", errors="ignore").strip()
            raise RuntimeError(f"gwt-video failed: {message or process.returncode}")
        if not output_path.exists() or output_path.stat().st_size <= 0:
            raise RuntimeError("gwt-video did not produce an output file")

        debug_logger.log_info(f"[GWT] processed video: {output_path.name}")

    async def _upload_processed_video(self, output_path: Path) -> str:
        debug_logger.log_info(f"[GWT] uploading processed video to S3: {output_path.name}")
        return await self._s3_uploader.upload_file(output_path, content_type="video/mp4")
