"""Minimal S3-compatible uploader for processed Flow2API videos."""

import hashlib
import hmac
import mimetypes
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import quote, urlparse, urlunparse

from curl_cffi.requests import AsyncSession

from ..core.config import config


@dataclass
class S3UploadConfig:
    enabled: bool
    endpoint: str
    region: str
    bucket: str
    access_key: str
    secret_key: str
    prefix: str
    public_base_url: str
    force_path_style: bool
    acl: str


class S3Uploader:
    """Upload processed files to S3 or an S3-compatible storage service."""

    def ensure_available(self) -> None:
        self._validate_config(self._load_config())

    async def upload_file(self, path: Path, *, content_type: Optional[str] = None) -> str:
        cfg = self._load_config()
        self._validate_config(cfg)

        key = self._object_key(path, cfg)
        data = path.read_bytes()
        content_type = content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        url = self._put_url(cfg, key)
        headers = self._signed_headers(
            cfg=cfg,
            method="PUT",
            url=url,
            body=data,
            content_type=content_type,
        )

        async with AsyncSession() as session:
            response = await session.put(
                url,
                data=data,
                headers=headers,
                timeout=300,
            )
        if response.status_code >= 300:
            raise RuntimeError(
                f"S3 upload failed: HTTP {response.status_code}: {response.text[:300]}"
            )

        return self._public_url(cfg, key)

    def _load_config(self) -> S3UploadConfig:
        return S3UploadConfig(
            enabled=self._env_bool("FLOW2API_S3_ENABLED", config.watermark_s3_enabled),
            endpoint=(os.getenv("FLOW2API_S3_ENDPOINT") or config.watermark_s3_endpoint).strip(),
            region=(os.getenv("FLOW2API_S3_REGION") or config.watermark_s3_region or "auto").strip(),
            bucket=(os.getenv("FLOW2API_S3_BUCKET") or config.watermark_s3_bucket).strip(),
            access_key=(os.getenv("FLOW2API_S3_ACCESS_KEY") or config.watermark_s3_access_key).strip(),
            secret_key=os.getenv("FLOW2API_S3_SECRET_KEY") or config.watermark_s3_secret_key,
            prefix=(os.getenv("FLOW2API_S3_PREFIX") or config.watermark_s3_prefix).strip(),
            public_base_url=(
                os.getenv("FLOW2API_S3_PUBLIC_BASE_URL")
                or config.watermark_s3_public_base_url
            ).strip(),
            force_path_style=self._env_bool(
                "FLOW2API_S3_FORCE_PATH_STYLE",
                config.watermark_s3_force_path_style,
            ),
            acl=(os.getenv("FLOW2API_S3_ACL") or config.watermark_s3_acl).strip(),
        )

    def _validate_config(self, cfg: S3UploadConfig) -> None:
        if not cfg.enabled:
            raise RuntimeError("S3 upload is disabled")
        missing = [
            name
            for name, value in (
                ("bucket", cfg.bucket),
                ("access_key", cfg.access_key),
                ("secret_key", cfg.secret_key),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(f"S3 upload config missing: {', '.join(missing)}")

    def _object_key(self, path: Path, cfg: S3UploadConfig) -> str:
        prefix = cfg.prefix.strip("/")
        return f"{prefix}/{path.name}" if prefix else path.name

    def _put_url(self, cfg: S3UploadConfig, key: str) -> str:
        escaped_key = self._quote_key(key)
        if cfg.endpoint:
            endpoint = cfg.endpoint.rstrip("/")
            parsed = urlparse(endpoint)
            if not parsed.scheme or not parsed.netloc:
                raise RuntimeError(f"Invalid S3 endpoint: {cfg.endpoint}")
            if cfg.force_path_style:
                return f"{endpoint}/{quote(cfg.bucket, safe='')}/{escaped_key}"
            return self._join_virtual_host_url(parsed, cfg.bucket, escaped_key)

        region = cfg.region or "us-east-1"
        if cfg.force_path_style:
            return f"https://s3.{region}.amazonaws.com/{quote(cfg.bucket, safe='')}/{escaped_key}"
        return f"https://{cfg.bucket}.s3.{region}.amazonaws.com/{escaped_key}"

    def _public_url(self, cfg: S3UploadConfig, key: str) -> str:
        escaped_key = self._quote_key(key)
        if cfg.public_base_url:
            return f"{cfg.public_base_url.rstrip('/')}/{escaped_key}"
        return self._put_url(cfg, key)

    def _signed_headers(
        self,
        *,
        cfg: S3UploadConfig,
        method: str,
        url: str,
        body: bytes,
        content_type: str,
    ) -> Dict[str, str]:
        parsed = urlparse(url)
        now = datetime.now(timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")
        payload_hash = hashlib.sha256(body).hexdigest()
        region = cfg.region or "auto"

        headers: Dict[str, str] = {
            "content-type": content_type,
            "host": parsed.netloc,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
        }
        if cfg.acl:
            headers["x-amz-acl"] = cfg.acl

        canonical_headers = "".join(
            f"{key}:{self._normalize_header_value(headers[key])}\n"
            for key in sorted(headers)
        )
        signed_headers = ";".join(sorted(headers))
        canonical_uri = parsed.path or "/"
        canonical_request = "\n".join(
            [
                method,
                canonical_uri,
                parsed.query or "",
                canonical_headers,
                signed_headers,
                payload_hash,
            ]
        )
        credential_scope = f"{date_stamp}/{region}/s3/aws4_request"
        string_to_sign = "\n".join(
            [
                "AWS4-HMAC-SHA256",
                amz_date,
                credential_scope,
                hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
            ]
        )
        signature = hmac.new(
            self._signing_key(cfg.secret_key, date_stamp, region),
            string_to_sign.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        headers["authorization"] = (
            "AWS4-HMAC-SHA256 "
            f"Credential={cfg.access_key}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, "
            f"Signature={signature}"
        )
        return headers

    @staticmethod
    def _signing_key(secret_key: str, date_stamp: str, region: str) -> bytes:
        date_key = hmac.new(
            f"AWS4{secret_key}".encode("utf-8"),
            date_stamp.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        region_key = hmac.new(date_key, region.encode("utf-8"), hashlib.sha256).digest()
        service_key = hmac.new(region_key, b"s3", hashlib.sha256).digest()
        return hmac.new(service_key, b"aws4_request", hashlib.sha256).digest()

    @staticmethod
    def _quote_key(key: str) -> str:
        return quote(key.lstrip("/"), safe="/-_.~")

    @staticmethod
    def _join_virtual_host_url(parsed, bucket: str, escaped_key: str) -> str:
        netloc = f"{bucket}.{parsed.netloc}"
        base_path = parsed.path.strip("/")
        path = f"/{base_path}/{escaped_key}" if base_path else f"/{escaped_key}"
        return urlunparse((parsed.scheme, netloc, path, "", "", ""))

    @staticmethod
    def _normalize_header_value(value: str) -> str:
        return " ".join(str(value).strip().split())

    @staticmethod
    def _env_bool(name: str, default: bool) -> bool:
        value = os.getenv(name)
        if value is None:
            return bool(default)
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
