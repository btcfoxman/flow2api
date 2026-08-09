"""API routes for OpenAI-compatible and Gemini generateContent endpoints."""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set
import asyncio
import base64
import json
import mimetypes
import re
import time
import uuid
from urllib.parse import unquote, urlparse

from curl_cffi.requests import AsyncSession
from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import AliasChoices, BaseModel, Field

from ..core.auth import AuthManager, verify_api_key_flexible
from ..core.logger import debug_logger
from ..core.media_errors import sanitize_public_error_message
from ..core.model_resolver import extract_generation_params, get_base_model_aliases, resolve_model_name
from ..core.models import (
    ChatCompletionRequest,
    ChatMessage,
    GeminiContent,
    GeminiGenerateContentRequest,
    Task,
)
from ..services.generation_handler import (
    MODEL_CONFIG,
    GenerationHandler,
    _validate_reference_video_duration,
)
from ..services.browser_captcha_extension import ExtensionCaptchaService

router = APIRouter()

MARKDOWN_IMAGE_RE = re.compile(r"!\[.*?\]\((.*?)\)")
HTML_VIDEO_RE = re.compile(r"<video[^>]+src=['\"](.*?)['\"]", re.IGNORECASE)
DATA_URL_RE = re.compile(r"^data:(?P<mime>[^;]+);base64,(?P<data>.+)$", re.DOTALL)
MEDIA_PROMPT_TOOL_BLOCK_RE = re.compile(r"<tools>.*?</tools>", re.IGNORECASE | re.DOTALL)
MEDIA_SYSTEM_INSTRUCTION_MARKERS = (
    "<tools>",
    "</tools>",
    "function calling ai model",
    "function signatures",
    "\"$schema\"",
    "\"additionalproperties\"",
)
VIDEO_IMAGE_PAYLOAD_KEYS = [
    "input_reference",
    "image",
    "image_url",
    "images",
    "image_urls",
    "reference_image",
    "reference_image_url",
    "reference_images",
    "reference_image_urls",
]
VIDEO_VIDEO_PAYLOAD_KEYS = [
    "video_url",
    "video_urls",
    "videos",
    "reference_video",
    "reference_video_url",
]
IMAGE_RESPONSE_TASKS: Dict[str, Dict[str, Any]] = {}
IMAGE_RESPONSE_TASKS_LOCK = asyncio.Lock()
ROUTE_BACKGROUND_TASKS: Set[asyncio.Task] = set()


def _spawn_route_background_task(coro: Any) -> asyncio.Task:
    """Keep fire-and-forget route work alive until it reaches a terminal state."""
    task = asyncio.create_task(coro)
    ROUTE_BACKGROUND_TASKS.add(task)
    task.add_done_callback(ROUTE_BACKGROUND_TASKS.discard)
    return task


MEDIA_PROMPT_PREAMBLE_PATTERNS = (
    re.compile(r"^you are a function calling ai model\.?$", re.IGNORECASE),
    re.compile(
        r"^you are provided with function signatures within .* xml tags\.?$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^you may call one or more functions to assist with the user query\.?$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^don't make assumptions about what values to plug into functions\.?$",
        re.IGNORECASE,
    ),
    re.compile(r"^here are the available tools:.*$", re.IGNORECASE),
)
GEMINI_STATUS_MAP = {
    400: "INVALID_ARGUMENT",
    401: "UNAUTHENTICATED",
    403: "PERMISSION_DENIED",
    404: "NOT_FOUND",
    409: "ABORTED",
    429: "RESOURCE_EXHAUSTED",
    500: "INTERNAL",
    502: "UNAVAILABLE",
    503: "UNAVAILABLE",
    504: "DEADLINE_EXCEEDED",
}

# Dependency injection will be set up in main.py
generation_handler: GenerationHandler = None


@dataclass
class NormalizedGenerationRequest:
    """Internal request shape shared by OpenAI and Gemini entrypoints."""

    model: str
    prompt: str
    images: List[bytes]
    messages: Optional[List[ChatMessage]] = None
    video_media_id: Optional[str] = None
    video_bytes: Optional[bytes] = None
    video_mime_type: Optional[str] = None
    video_file_name: Optional[str] = None
    aspect_ratio_override: Optional[str] = None
    watermark: bool = False


class WatermarkRemovalRequest(BaseModel):
    source_url: str = Field(
        min_length=1,
        max_length=4096,
        validation_alias=AliasChoices("source_url", "video_url"),
    )


class WatermarkRemovalResponse(BaseModel):
    id: str
    object: str = "video.watermark_removal"
    status: str = "completed"
    source_url: str
    video_url: str


def set_generation_handler(handler: GenerationHandler):
    """Set generation handler instance."""
    global generation_handler
    generation_handler = handler


def _ensure_generation_handler() -> GenerationHandler:
    if generation_handler is None:
        raise HTTPException(status_code=500, detail="Generation handler not initialized")
    return generation_handler


def _build_model_description(model_config: Dict[str, Any]) -> str:
    """Build a human-readable description for model listing endpoints."""
    description = f"{model_config['type'].capitalize()} generation"
    if model_config["type"] == "image":
        description += f" - {model_config['model_name']}"
    else:
        description += f" - {model_config['model_key']}"
    return description


def _get_openai_model_catalog() -> List[Dict[str, str]]:
    """Collect OpenAI-compatible model list entries."""
    return [
        {
            "id": model_id,
            "description": _build_model_description(model_config),
        }
        for model_id, model_config in MODEL_CONFIG.items()
    ]


def _get_gemini_model_catalog() -> Dict[str, str]:
    """Collect Gemini-compatible model metadata for /models endpoints."""
    catalog: Dict[str, str] = {}

    for alias_id, description in get_base_model_aliases().items():
        catalog[alias_id] = description

    for model_id, model_config in MODEL_CONFIG.items():
        catalog.setdefault(model_id, _build_model_description(model_config))

    return catalog


def _build_gemini_model_resource(model_id: str, description: str) -> Dict[str, Any]:
    """Build a Gemini-compatible model resource payload."""
    return {
        "name": f"models/{model_id}",
        "displayName": model_id,
        "description": description,
        "version": "flow2api",
        "inputTokenLimit": 0,
        "outputTokenLimit": 0,
        "supportedGenerationMethods": [
            "generateContent",
            "streamGenerateContent",
        ],
    }


def _decode_data_url(data_url: str) -> tuple[str, bytes]:
    match = DATA_URL_RE.match(data_url)
    if not match:
        raise HTTPException(status_code=400, detail="Invalid data URL")
    return match.group("mime"), base64.b64decode(match.group("data"))


def _detect_image_mime_type(image_bytes: bytes, fallback: str = "image/png") -> str:
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image_bytes.startswith(b"GIF87a") or image_bytes.startswith(b"GIF89a"):
        return "image/gif"
    if image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    return fallback


def _guess_mime_type(uri: str, fallback: str) -> str:
    guessed, _ = mimetypes.guess_type(urlparse(uri).path)
    return guessed or fallback


async def retrieve_image_data(url: str) -> Optional[bytes]:
    """Read image bytes from local /tmp cache or remote URL."""
    file_cache = getattr(generation_handler, "file_cache", None)
    try:
        if "/tmp/" in url and file_cache:
            path = urlparse(url).path
            filename = path.split("/tmp/")[-1]
            local_file_path = file_cache.cache_dir / filename

            if local_file_path.exists() and local_file_path.is_file():
                data = local_file_path.read_bytes()
                if data:
                    return data
    except Exception as exc:
        debug_logger.log_warning(f"[CONTEXT] 本地缓存读取失败: {str(exc)}")

    proxy_url = None
    try:
        if file_cache and hasattr(file_cache, "_resolve_download_proxy"):
            proxy_url = await file_cache._resolve_download_proxy("image")
    except Exception as exc:
        debug_logger.log_warning(f"[CONTEXT] 图片下载代理解析失败: {str(exc)}")

    try:
        async with AsyncSession() as session:
            response = await session.get(
                url,
                timeout=60,
                proxies={"http": proxy_url, "https": proxy_url} if proxy_url else None,
                headers={
                    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                    "Accept-Encoding": "gzip, deflate, br",
                    "Connection": "keep-alive",
                    "Referer": "https://labs.google/",
                },
                impersonate="chrome120",
                verify=False,
            )
            if response.status_code == 200 and response.content:
                return response.content
            debug_logger.log_warning(
                f"[CONTEXT] 图片下载失败，状态码: {response.status_code}"
            )
    except Exception as exc:
        debug_logger.log_error(f"[CONTEXT] 图片下载异常: {str(exc)}")

    return None


async def retrieve_video_data(url: str) -> Optional[bytes]:
    """Read video bytes from local /tmp cache or remote URL."""
    file_cache = getattr(generation_handler, "file_cache", None)
    try:
        if "/tmp/" in url and file_cache:
            path = urlparse(url).path
            filename = path.split("/tmp/")[-1]
            local_file_path = file_cache.cache_dir / filename

            if local_file_path.exists() and local_file_path.is_file():
                data = local_file_path.read_bytes()
                if data:
                    return data
    except Exception as exc:
        debug_logger.log_warning(f"[CONTEXT] local video cache read failed: {str(exc)}")

    proxy_url = None
    try:
        if file_cache and hasattr(file_cache, "_resolve_download_proxy"):
            proxy_url = await file_cache._resolve_download_proxy("video")
    except Exception as exc:
        debug_logger.log_warning(f"[CONTEXT] video download proxy resolve failed: {str(exc)}")

    try:
        async with AsyncSession() as session:
            response = await session.get(
                url,
                timeout=120,
                proxies={"http": proxy_url, "https": proxy_url} if proxy_url else None,
                headers={
                    "Accept": "video/mp4,video/*,*/*;q=0.8",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                    "Accept-Encoding": "identity;q=1, *;q=0",
                    "Connection": "keep-alive",
                    "Referer": "https://labs.google/",
                },
                impersonate="chrome120",
                verify=False,
            )
            if response.status_code == 200 and response.content:
                return response.content
            debug_logger.log_warning(f"[CONTEXT] video download failed, status={response.status_code}")
    except Exception as exc:
        debug_logger.log_error(f"[CONTEXT] video download exception: {str(exc)}")

    return None


async def _load_image_bytes_from_uri(uri: str) -> bytes:
    if not uri:
        raise HTTPException(status_code=400, detail="Image URI cannot be empty")

    if uri.startswith("data:image"):
        _, image_bytes = _decode_data_url(uri)
        return image_bytes

    if uri.startswith("http://") or uri.startswith("https://") or "/tmp/" in uri:
        image_bytes = await retrieve_image_data(uri)
        if image_bytes:
            return image_bytes
        raise HTTPException(status_code=400, detail=f"Failed to load image from {uri}")

    raise HTTPException(status_code=400, detail=f"Unsupported image URI: {uri}")


async def _load_video_bytes_from_uri(uri: str) -> tuple[bytes, str, str]:
    if not uri:
        raise HTTPException(status_code=400, detail="Video URI cannot be empty")

    if uri.startswith("data:"):
        mime_type, video_bytes = _decode_data_url(uri)
        if not (mime_type.startswith("video/") or mime_type == "application/octet-stream"):
            raise HTTPException(status_code=400, detail=f"Unsupported video data URL mime type: {mime_type}")
        extension = mimetypes.guess_extension(mime_type) or ".mp4"
        return video_bytes, mime_type, f"upload{extension}"

    if uri.startswith("http://") or uri.startswith("https://") or "/tmp/" in uri:
        video_bytes = await retrieve_video_data(uri)
        if video_bytes:
            mime_type = _guess_mime_type(uri, "video/mp4")
            file_name = urlparse(uri).path.rsplit("/", 1)[-1] or "upload.mp4"
            return video_bytes, mime_type, file_name
        raise HTTPException(status_code=400, detail=f"Failed to load video from {uri}")

    raise HTTPException(status_code=400, detail=f"Unsupported video URI: {uri}")


def _decode_video_payload(value: str) -> tuple[bytes, str, str]:
    if not value:
        raise HTTPException(status_code=400, detail="Video payload cannot be empty")

    if value.startswith("data:"):
        mime_type, video_bytes = _decode_data_url(value)
        if not (mime_type.startswith("video/") or mime_type == "application/octet-stream"):
            raise HTTPException(status_code=400, detail=f"Unsupported video mime type: {mime_type}")
        extension = mimetypes.guess_extension(mime_type) or ".mp4"
        return video_bytes, mime_type, f"upload{extension}"

    return base64.b64decode(value), "video/mp4", "upload.mp4"


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "y", "on"}:
        return True
    if text in {"false", "0", "no", "n", "off"}:
        return False
    return default


def _extract_url_value(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, dict):
        for key in ("url", "uri", "image_url", "video_url", "fileUri", "file_uri"):
            nested = _extract_url_value(value.get(key))
            if nested:
                return nested
    return None


def _collect_media_values(payload: Dict[str, Any], keys: List[str]) -> List[str]:
    values: List[str] = []

    def add(value: Any) -> None:
        if value is None:
            return
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return
            try:
                parsed = json.loads(stripped)
            except Exception:
                values.append(stripped)
                return
            add(parsed)
            return
        if isinstance(value, list):
            for item in value:
                add(item)
            return
        extracted = _extract_url_value(value)
        if extracted:
            values.append(extracted)

    for key in keys:
        add(payload.get(key))
    return values


def _parse_jsonish_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text or text[0] not in "[{":
        return value
    try:
        return json.loads(text)
    except Exception:
        return value


def _append_video_payload_media_items(
    payload: Dict[str, Any], content: List[Dict[str, Any]]
) -> None:
    for image_url in _collect_media_values(payload, VIDEO_IMAGE_PAYLOAD_KEYS):
        content.append({"type": "image_url", "image_url": {"url": image_url}})
    for video_url in _collect_media_values(payload, VIDEO_VIDEO_PAYLOAD_KEYS):
        content.append({"type": "video_url", "video_url": {"url": video_url}})


def _video_aspect_override_from_request(request: Any) -> Optional[str]:
    aspect_ratio, _ = extract_generation_params(request)
    if aspect_ratio == "landscape":
        return "VIDEO_ASPECT_RATIO_LANDSCAPE"
    if aspect_ratio == "portrait":
        return "VIDEO_ASPECT_RATIO_PORTRAIT"
    return None


def _coerce_gemini_contents(raw_contents: Optional[List[Any]]) -> List[GeminiContent]:
    contents: List[GeminiContent] = []
    for item in raw_contents or []:
        if isinstance(item, GeminiContent):
            contents.append(item)
        else:
            contents.append(GeminiContent.model_validate(item))
    return contents


def _extract_text_from_gemini_content(content: Optional[GeminiContent]) -> str:
    if content is None:
        return ""
    text_parts = [part.text.strip() for part in content.parts if part.text]
    return "\n".join(part for part in text_parts if part).strip()


def _should_ignore_media_system_instruction(system_instruction: str) -> bool:
    """Drop agent/tool scaffolding before sending media prompts upstream."""
    if not system_instruction:
        return False

    normalized = system_instruction.lower()
    if len(system_instruction) > 1200:
        return True

    return any(marker in normalized for marker in MEDIA_SYSTEM_INSTRUCTION_MARKERS)


def _sanitize_media_prompt(prompt: str) -> str:
    """Strip agent/tool scaffolding that image/video models cannot use."""
    if not prompt:
        return ""

    sanitized = MEDIA_PROMPT_TOOL_BLOCK_RE.sub(" ", prompt.strip())
    cleaned_lines: List[str] = []
    for raw_line in sanitized.splitlines():
        line = raw_line.strip()
        if not line:
            if cleaned_lines and cleaned_lines[-1] != "":
                cleaned_lines.append("")
            continue
        if any(pattern.fullmatch(line) for pattern in MEDIA_PROMPT_PREAMBLE_PATTERNS):
            continue
        cleaned_lines.append(line)

    sanitized = "\n".join(cleaned_lines).strip()
    sanitized = re.sub(r"\n{3,}", "\n\n", sanitized)
    return sanitized.strip()


async def _extract_prompt_and_images_from_openai_messages(
    messages: List[ChatMessage],
) -> tuple[str, List[bytes], Optional[str], Optional[bytes], Optional[str], Optional[str]]:
    """Extract prompt and media from OpenAI-compatible messages."""
    last_message = messages[-1]
    content = last_message.content
    prompt_parts: List[str] = []
    images: List[bytes] = []
    video_media_id: Optional[str] = None
    video_bytes: Optional[bytes] = None
    video_mime_type: Optional[str] = None
    video_file_name: Optional[str] = None

    if isinstance(content, str):
        prompt_parts.append(content)
    elif isinstance(content, list):
        for item in content:
            item_type = item.get("type")
            if item_type == "text":
                text = item.get("text", "").strip()
                if text:
                    prompt_parts.append(text)
            elif item_type == "image_url":
                image_url = _extract_url_value(item.get("image_url")) or ""
                # extend://MEDIA_ID 用于视频续写
                if image_url.startswith("extend://"):
                    video_media_id = image_url[len("extend://"):]
                else:
                    images.append(await _load_image_bytes_from_uri(image_url))
            elif item_type == "video_url":
                video_url = _extract_url_value(item.get("video_url")) or ""
                if video_url.startswith("extend://"):
                    video_media_id = video_url[len("extend://"):]
                else:
                    video_bytes, video_mime_type, video_file_name = await _load_video_bytes_from_uri(video_url)

    prompt = "\n".join(part for part in prompt_parts if part).strip()
    return prompt, images, video_media_id, video_bytes, video_mime_type, video_file_name


async def _append_openai_reference_images(
    model: str,
    messages: List[ChatMessage],
    images: List[bytes],
) -> List[bytes]:
    model_config = MODEL_CONFIG.get(model)
    if not model_config or model_config["type"] != "image" or len(messages) <= 1:
        return images

    debug_logger.log_info(f"[CONTEXT] 开始查找历史参考图，消息数量: {len(messages)}")

    for msg in reversed(messages[:-1]):
        if msg.role == "assistant" and isinstance(msg.content, str):
            matches = MARKDOWN_IMAGE_RE.findall(msg.content)
            if not matches:
                continue

            for image_url in reversed(matches):
                if not image_url.startswith("http") and "/tmp/" not in image_url:
                    continue
                try:
                    downloaded_bytes = await retrieve_image_data(image_url)
                    if downloaded_bytes:
                        images.insert(0, downloaded_bytes)
                        debug_logger.log_info(
                            f"[CONTEXT] ✅ 添加历史参考图: {image_url}"
                        )
                        return images
                    debug_logger.log_warning(
                        f"[CONTEXT] 图片下载失败或为空，尝试下一个: {image_url}"
                    )
                except Exception as exc:
                    debug_logger.log_error(
                        f"[CONTEXT] 处理参考图时出错: {str(exc)}"
                    )
    return images


async def _extract_prompt_and_images_from_gemini_contents(
    contents: List[GeminiContent],
) -> tuple[str, List[bytes], Optional[bytes], Optional[str], Optional[str]]:
    if not contents:
        raise HTTPException(status_code=400, detail="contents cannot be empty")

    target_content = next(
        (content for content in reversed(contents) if (content.role or "user") == "user"),
        contents[-1],
    )

    prompt_parts: List[str] = []
    images: List[bytes] = []
    video_bytes: Optional[bytes] = None
    video_mime_type: Optional[str] = None
    video_file_name: Optional[str] = None

    for part in target_content.parts:
        if part.text:
            text = part.text.strip()
            if text:
                prompt_parts.append(text)
        elif part.inlineData is not None:
            mime_type = part.inlineData.mimeType.lower()
            if mime_type.startswith("image/"):
                images.append(base64.b64decode(part.inlineData.data))
            elif mime_type.startswith("video/") or mime_type == "application/octet-stream":
                video_bytes = base64.b64decode(part.inlineData.data)
                video_mime_type = part.inlineData.mimeType
                extension = mimetypes.guess_extension(video_mime_type) or ".mp4"
                video_file_name = f"upload{extension}"
            else:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unsupported inlineData mime type: {part.inlineData.mimeType}",
                )
        elif part.fileData is not None:
            mime_type = (part.fileData.mimeType or "").lower()
            if not mime_type:
                mime_type = _guess_mime_type(part.fileData.fileUri, "").lower()
            if mime_type.startswith("video/") or mime_type == "application/octet-stream":
                video_bytes, video_mime_type, video_file_name = await _load_video_bytes_from_uri(part.fileData.fileUri)
            elif not mime_type or mime_type.startswith("image/"):
                images.append(await _load_image_bytes_from_uri(part.fileData.fileUri))
            else:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unsupported fileData mime type: {part.fileData.mimeType}",
                )

    prompt = "\n".join(part for part in prompt_parts if part).strip()
    return prompt, images, video_bytes, video_mime_type, video_file_name


def _resolve_request_model(model: str, request: Any) -> str:
    resolved_model = resolve_model_name(model=model, request=request, model_config=MODEL_CONFIG)
    if resolved_model != model:
        debug_logger.log_info(f"[ROUTE] 模型名已转换: {model} → {resolved_model}")
    return resolved_model


def _get_request_base_url(request: Request) -> Optional[str]:
    """根据实际请求头推导对外可访问的基础地址。"""
    forwarded_proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    forwarded_host = (request.headers.get("x-forwarded-host") or "").split(",")[0].strip()
    host = (forwarded_host or request.headers.get("host") or "").strip()

    if not host:
        return None

    proto = forwarded_proto or request.url.scheme or "http"
    return f"{proto}://{host}"


async def _normalize_openai_request(
    request: ChatCompletionRequest,
) -> NormalizedGenerationRequest:
    if request.messages:
        (
            prompt,
            images,
            video_media_id,
            video_bytes,
            video_mime_type,
            video_file_name,
        ) = await _extract_prompt_and_images_from_openai_messages(
            request.messages
        )
        if request.image and not images:
            images.append(await _load_image_bytes_from_uri(request.image))
        if request.video and not video_media_id and video_bytes is None:
            video_bytes, video_mime_type, video_file_name = _decode_video_payload(request.video)
        model = _resolve_request_model(request.model, request)
        model_config = MODEL_CONFIG.get(model)
        if model_config and model_config.get("type") in {"image", "video"}:
            prompt = _sanitize_media_prompt(prompt)
        images = await _append_openai_reference_images(model, request.messages, images)
        return NormalizedGenerationRequest(
            model=model,
            prompt=prompt,
            images=images,
            messages=request.messages,
            video_media_id=video_media_id,
            video_bytes=video_bytes,
            video_mime_type=video_mime_type,
            video_file_name=video_file_name,
            aspect_ratio_override=_video_aspect_override_from_request(request),
            watermark=_coerce_bool(getattr(request, "watermark", None), False),
        )

    if request.contents:
        gemini_request = GeminiGenerateContentRequest(
            contents=_coerce_gemini_contents(request.contents),
            generationConfig=request.generationConfig,
        )
        normalized = await _normalize_gemini_request(request.model, gemini_request)
        normalized.messages = request.messages
        if request.video and not normalized.video_media_id and normalized.video_bytes is None:
            (
                normalized.video_bytes,
                normalized.video_mime_type,
                normalized.video_file_name,
            ) = _decode_video_payload(request.video)
        normalized.watermark = _coerce_bool(getattr(request, "watermark", None), False)
        return normalized

    raise HTTPException(status_code=400, detail="Messages or contents cannot be empty")


async def _normalize_gemini_request(
    model: str,
    request: GeminiGenerateContentRequest,
) -> NormalizedGenerationRequest:
    resolved_model = _resolve_request_model(model, request)
    prompt, images, video_bytes, video_mime_type, video_file_name = await _extract_prompt_and_images_from_gemini_contents(request.contents)
    system_instruction = _extract_text_from_gemini_content(request.systemInstruction)
    model_config = MODEL_CONFIG.get(resolved_model)
    media_model = bool(model_config and model_config.get("type") in {"image", "video"})

    if media_model:
        prompt = _sanitize_media_prompt(prompt)

    if system_instruction:
        if media_model and _should_ignore_media_system_instruction(system_instruction):
            debug_logger.log_warning(
                f"[GEMINI] 忽略媒体模型的 systemInstruction: model={resolved_model}, len={len(system_instruction)}"
            )
        else:
            if media_model:
                system_instruction = _sanitize_media_prompt(system_instruction)
            prompt = f"{system_instruction}\n\n{prompt}".strip()

    return NormalizedGenerationRequest(
        model=resolved_model,
        prompt=prompt,
        images=images,
        video_bytes=video_bytes,
        video_mime_type=video_mime_type,
        video_file_name=video_file_name,
        aspect_ratio_override=_video_aspect_override_from_request(request),
        watermark=_coerce_bool(getattr(request, "watermark", None), False),
    )


async def _read_request_payload(raw_request: Request) -> Dict[str, Any]:
    content_type = (raw_request.headers.get("content-type") or "").lower()
    if "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
        form = await raw_request.form()
        return {key: value for key, value in form.items()}

    raw_body = await raw_request.body()
    if not raw_body:
        return {}
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON body: {exc}")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object")
    return payload


async def _normalize_video_create_payload(payload: Dict[str, Any]) -> NormalizedGenerationRequest:
    if payload.get("messages") or payload.get("contents"):
        return await _normalize_openai_request(ChatCompletionRequest.model_validate(payload))

    model = str(payload.get("model") or "").strip()
    raw_content = _parse_jsonish_value(payload.get("content"))
    prompt = str(payload.get("prompt") or "").strip()
    if not prompt and isinstance(raw_content, str):
        prompt = raw_content.strip()
    if not model:
        raise HTTPException(status_code=400, detail="model is required")
    if not prompt and not isinstance(raw_content, (list, dict)):
        raise HTTPException(status_code=400, detail="prompt is required")

    if isinstance(raw_content, (list, dict)):
        content_items = list(raw_content) if isinstance(raw_content, list) else [raw_content]
        if prompt and not any(
            isinstance(item, dict) and item.get("type") == "text"
            for item in content_items
        ):
            content_items = [{"type": "text", "text": prompt}, *content_items]
        _append_video_payload_media_items(payload, content_items)
        request_payload = dict(payload)
        request_payload["model"] = model
        request_payload["messages"] = [
            {
                "role": "user",
                "content": content_items,
            }
        ]
        request = ChatCompletionRequest.model_validate(request_payload)
        return await _normalize_openai_request(request)

    content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
    _append_video_payload_media_items(payload, content)

    request_payload = dict(payload)
    request_payload["model"] = model
    request_payload["messages"] = [
        {
            "role": "user",
            "content": content if len(content) > 1 else prompt,
        }
    ]
    request = ChatCompletionRequest.model_validate(request_payload)
    return await _normalize_openai_request(request)


def _to_seedance_task_status(status: Any) -> str:
    normalized = str(status or "").strip().lower()
    if normalized in {"completed", "success", "succeeded", "done", "finished"}:
        return "succeeded"
    if normalized in {"failed", "fail", "error", "cancelled", "canceled"}:
        return "failed"
    if normalized in {"processing", "in_progress", "running", "working"}:
        return "running"
    return "queued"


def _format_seedance_task_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    formatted: Dict[str, Any] = {
        "id": payload.get("id", ""),
        "status": _to_seedance_task_status(payload.get("status")),
    }
    for source_key, target_key in (
        ("model", "model"),
        ("created_at", "created_at"),
        ("completed_at", "updated_at"),
        ("progress", "progress"),
    ):
        if payload.get(source_key) is not None:
            formatted[target_key] = payload[source_key]
    content: Dict[str, Any] = {}
    if payload.get("video_url"):
        content["video_url"] = payload["video_url"]
    if content:
        formatted["content"] = content
    if payload.get("error") is not None:
        formatted["error"] = payload["error"]
    return formatted


async def _collect_non_stream_result(
    model: str,
    prompt: str,
    images: List[bytes],
    base_url_override: Optional[str] = None,
    video_media_id: Optional[str] = None,
    video_bytes: Optional[bytes] = None,
    video_mime_type: Optional[str] = None,
    video_file_name: Optional[str] = None,
    aspect_ratio_override: Optional[str] = None,
    watermark: bool = False,
) -> str:
    handler = _ensure_generation_handler()
    result = None
    async for chunk in handler.handle_generation(
        model=model,
        prompt=prompt,
        images=images if images else None,
        stream=False,
        base_url_override=base_url_override,
        video_media_id=video_media_id,
        video_bytes=video_bytes,
        video_mime_type=video_mime_type,
        video_file_name=video_file_name,
        aspect_ratio_override=aspect_ratio_override,
        watermark=watermark,
    ):
        result = chunk

    if result is None:
        raise HTTPException(status_code=500, detail="Generation failed: No response")

    return result


async def _collect_async_video_task_result(
    normalized: NormalizedGenerationRequest,
    base_url_override: Optional[str] = None,
) -> Dict[str, Any]:
    handler = _ensure_generation_handler()
    result = None
    async for chunk in handler.handle_generation(
        model=normalized.model,
        prompt=normalized.prompt,
        images=normalized.images if normalized.images else None,
        stream=False,
        base_url_override=base_url_override,
        video_media_id=normalized.video_media_id,
        video_bytes=normalized.video_bytes,
        video_mime_type=normalized.video_mime_type,
        video_file_name=normalized.video_file_name,
        aspect_ratio_override=normalized.aspect_ratio_override,
        async_video_task=True,
        watermark=normalized.watermark,
    ):
        result = chunk

    if result is None:
        raise HTTPException(status_code=500, detail="Video task creation failed: No response")

    return _parse_handler_result(result)


def _video_model_response_name(model: str) -> str:
    model_config = MODEL_CONFIG.get(model, {})
    return str(model_config.get("model_key") or model)


def _new_deferred_video_task_id() -> str:
    return f"flow2api-submit-{uuid.uuid4().hex}"


def _extract_error_message(payload: Dict[str, Any]) -> str:
    error = payload.get("error")
    if isinstance(error, dict):
        message = error.get("message") or error.get("error") or error.get("detail")
        if message:
            return sanitize_public_error_message(message)
    if isinstance(error, str) and error.strip():
        return sanitize_public_error_message(error)
    message = payload.get("message") or payload.get("detail")
    if message:
        return sanitize_public_error_message(message)
    return "视频任务提交失败，请稍后重试"


def _extract_upstream_video_task_id(payload: Dict[str, Any]) -> Optional[str]:
    for key in ("id", "task_id"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _video_task_validation_error(
    normalized: NormalizedGenerationRequest,
) -> Optional[Dict[str, Any]]:
    model_config = MODEL_CONFIG.get(normalized.model, {})
    video_type = model_config.get("video_type")
    min_images = int(model_config.get("min_images") or 0)
    max_images = model_config.get("max_images")
    max_images = int(max_images) if max_images is not None else None
    image_count = len(normalized.images or [])
    has_video_input = bool(normalized.video_media_id or normalized.video_bytes)
    message: Optional[str] = None

    if video_type == "i2v":
        if image_count < min_images or (max_images is not None and image_count > max_images):
            message = f"❌ 首尾帧模型需要 {min_images}-{max_images} 张图片,当前提供了 {image_count} 张"
    elif video_type == "r2v":
        if image_count < min_images or (max_images is not None and image_count > max_images):
            message = f"❌ 多图视频模型需要 {min_images}-{max_images} 张参考图,当前提供了 {image_count} 张"
    elif video_type == "v2v":
        if not has_video_input:
            message = "❌ 视频编辑模型需要提供参考视频"
        elif image_count < min_images or (max_images is not None and image_count > max_images):
            message = f"❌ 视频编辑模型需要 {min_images}-{max_images} 张参考图,当前提供了 {image_count} 张"
        elif normalized.video_bytes:
            try:
                _validate_reference_video_duration(normalized.video_bytes)
            except ValueError as exc:
                message = f"❌ {exc}"
    elif video_type == "extend" and not normalized.video_media_id:
        message = "❌ 视频续写需要提供源视频的 mediaGenerationId，请在 image_url 中传入 extend://VIDEO_MEDIA_ID"

    if not message:
        return None
    return {
        "error": {
            "message": message,
            "type": "invalid_request_error",
            "code": "generation_failed",
            "status_code": 400,
        }
    }


async def _create_deferred_async_video_task(
    normalized: NormalizedGenerationRequest,
    base_url_override: Optional[str] = None,
) -> Dict[str, Any]:
    handler = _ensure_generation_handler()
    local_task_id = _new_deferred_video_task_id()
    model_name = _video_model_response_name(normalized.model)
    token = await handler.load_balancer.select_token(
        for_video_generation=True,
        model=normalized.model,
        reserve=False,
        enforce_concurrency_filter=False,
        track_pending=False,
    )
    if token is None:
        message = None
        if hasattr(handler.load_balancer, "get_unavailable_reason"):
            message = await handler.load_balancer.get_unavailable_reason(
                for_video_generation=True,
                model=normalized.model,
            )
        return {
            "error": {
                "message": message or "当前没有额度充足的可用账号，暂无法生成视频。",
                "type": "server_error",
                "code": "generation_failed",
                "status_code": 503,
            }
        }
    if token.id is None:
        return {
            "error": {
                "message": "当前账号状态异常，暂无法生成视频。",
                "type": "server_error",
                "code": "generation_failed",
                "status_code": 503,
            }
        }

    await handler.db.create_task(
        Task(
            task_id=local_task_id,
            token_id=token.id,
            model=model_name,
            prompt=normalized.prompt,
            status="processing",
            progress=1,
            watermark=normalized.watermark,
        )
    )
    _spawn_route_background_task(
        _run_deferred_async_video_task(
            local_task_id=local_task_id,
            normalized=normalized,
            base_url_override=base_url_override,
        )
    )

    return {
        "id": local_task_id,
        "object": "video",
        "status": "processing",
        "created_at": int(time.time()),
        "model": model_name,
        "progress": 1,
    }


async def _run_deferred_async_video_task(
    *,
    local_task_id: str,
    normalized: NormalizedGenerationRequest,
    base_url_override: Optional[str],
) -> None:
    handler = _ensure_generation_handler()
    try:
        result = await _collect_async_video_task_result(normalized, base_url_override)
        if "error" in result:
            await handler.db.update_task(
                local_task_id,
                status="failed",
                progress=100,
                error_message=_extract_error_message(result),
                completed_at=time.time(),
            )
            return

        upstream_task_id = _extract_upstream_video_task_id(result)
        if not upstream_task_id:
            await handler.db.update_task(
                local_task_id,
                status="failed",
                progress=100,
                error_message="视频任务提交失败：生成服务未返回 task_id",
                completed_at=time.time(),
            )
            return

        await _mirror_upstream_video_task(
            local_task_id=local_task_id,
            upstream_task_id=upstream_task_id,
        )
    except Exception as exc:
        debug_logger.log_error(
            f"[VIDEO DEFERRED] background submit failed task={local_task_id}: {exc}"
        )
        await handler.db.update_task(
            local_task_id,
            status="failed",
            progress=100,
            error_message=sanitize_public_error_message(str(exc) or exc.__class__.__name__),
            completed_at=time.time(),
        )


async def _mirror_upstream_video_task(
    *,
    local_task_id: str,
    upstream_task_id: str,
    timeout_seconds: float = 7200.0,
    interval_seconds: float = 2.0,
) -> None:
    handler = _ensure_generation_handler()
    deadline = time.monotonic() + max(60.0, timeout_seconds)
    last_progress = 1

    while time.monotonic() < deadline:
        upstream_task = await handler.db.get_task(upstream_task_id)
        if upstream_task is None:
            await asyncio.sleep(interval_seconds)
            continue

        update_fields: Dict[str, Any] = {
            "token_id": upstream_task.token_id,
            "model": upstream_task.model,
            "status": upstream_task.status,
            "progress": max(int(upstream_task.progress or 0), last_progress),
            "scene_id": upstream_task.scene_id,
            "project_id": upstream_task.project_id,
            "watermark": upstream_task.watermark,
        }
        if upstream_task.operations:
            update_fields["operations"] = upstream_task.operations
        if upstream_task.result_urls:
            update_fields["result_urls"] = upstream_task.result_urls
        if upstream_task.error_message:
            update_fields["error_message"] = upstream_task.error_message
        if upstream_task.status in {"completed", "failed"}:
            update_fields["progress"] = 100
            update_fields["completed_at"] = time.time()

        last_progress = int(update_fields.get("progress") or last_progress)
        await handler.db.update_task(local_task_id, **update_fields)

        if upstream_task.status in {"completed", "failed"}:
            return

        await asyncio.sleep(interval_seconds)

    await handler.db.update_task(
        local_task_id,
        status="failed",
        progress=100,
        error_message="视频任务提交后同步结果超时",
        completed_at=time.time(),
    )


def _parse_handler_result(result: str) -> Dict[str, Any]:
    try:
        return json.loads(result)
    except json.JSONDecodeError:
        return {"result": result}


def _get_error_status_code(payload: Dict[str, Any]) -> int:
    error = payload.get("error")
    if isinstance(error, dict):
        status_code = error.get("status_code")
        if isinstance(status_code, int):
            return status_code
        if isinstance(status_code, str) and status_code.isdigit():
            return int(status_code)
        return 400
    return 200


def _build_openai_json_response(payload: Dict[str, Any]) -> JSONResponse:
    public_payload = payload
    error = payload.get("error")
    if isinstance(error, dict):
        public_payload = dict(payload)
        public_error = dict(error)
        if public_error.get("message"):
            public_error["message"] = sanitize_public_error_message(public_error["message"])
        public_payload["error"] = public_error
    elif isinstance(error, str):
        public_payload = dict(payload)
        public_payload["error"] = sanitize_public_error_message(error)
    return JSONResponse(content=public_payload, status_code=_get_error_status_code(public_payload))


def _new_image_response_id() -> str:
    return f"resp_{uuid.uuid4().hex}"


def _extract_responses_image_uri(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, dict):
        for key in ("url", "image_url", "uri", "src", "data", "fileUri"):
            uri = _extract_responses_image_uri(value.get(key))
            if uri:
                return uri
    return None


def _collect_responses_image_uris(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        uris: List[str] = []
        for item in value:
            uris.extend(_collect_responses_image_uris(item))
        return uris
    uri = _extract_responses_image_uri(value)
    return [uri] if uri else []


def _append_responses_input_parts(
    value: Any,
    prompt_parts: List[str],
    image_uris: List[str],
) -> None:
    if value is None:
        return
    if isinstance(value, str):
        text = value.strip()
        if text:
            prompt_parts.append(text)
        return
    if isinstance(value, list):
        for item in value:
            _append_responses_input_parts(item, prompt_parts, image_uris)
        return
    if not isinstance(value, dict):
        return

    item_type = str(value.get("type") or "").strip()
    if item_type in {"input_text", "text"}:
        text = value.get("text") or value.get("input_text")
        if isinstance(text, str) and text.strip():
            prompt_parts.append(text.strip())
    elif item_type in {"input_image", "image_url", "reference_image"}:
        image_uris.extend(
            _collect_responses_image_uris(value.get("image_url") or value.get("url") or value)
        )

    content = value.get("content")
    if isinstance(content, (list, dict, str)):
        _append_responses_input_parts(content, prompt_parts, image_uris)


def _responses_image_output_format(payload: Dict[str, Any]) -> str:
    candidates: List[Any] = [
        payload.get("response_format"),
        payload.get("output_format"),
        payload.get("format"),
    ]
    tools = payload.get("tools")
    if isinstance(tools, list):
        for tool in tools:
            if isinstance(tool, dict) and tool.get("type") in {
                "image_generation",
                "image_generation_call",
            }:
                candidates.extend(
                    [
                        tool.get("response_format"),
                        tool.get("output_format"),
                        tool.get("format"),
                    ]
                )
    for candidate in candidates:
        normalized = str(candidate or "").strip().lower()
        if normalized in {"b64_json", "base64", "base64_json"}:
            return "b64_json"
    return "url"


async def _normalize_responses_image_request(
    payload: Dict[str, Any],
) -> NormalizedGenerationRequest:
    model = str(payload.get("model") or "").strip()
    if not model:
        raise HTTPException(status_code=400, detail="model is required")

    prompt_parts: List[str] = []
    image_uris: List[str] = []
    prompt = payload.get("prompt")
    if isinstance(prompt, str) and prompt.strip():
        prompt_parts.append(prompt.strip())
    _append_responses_input_parts(payload.get("input"), prompt_parts, image_uris)

    for key in (
        "image",
        "image_url",
        "image_urls",
        "images",
        "input_image",
        "input_images",
        "input_reference",
        "input_references",
        "reference_image",
        "reference_images",
    ):
        image_uris.extend(_collect_responses_image_uris(payload.get(key)))

    prompt_text = "\n".join(part for part in prompt_parts if part).strip()
    if not prompt_text:
        raise HTTPException(status_code=400, detail="Prompt cannot be empty")

    unique_image_uris = list(dict.fromkeys([uri for uri in image_uris if uri]))
    content: List[Dict[str, Any]] = [{"type": "text", "text": prompt_text}]
    for uri in unique_image_uris:
        content.append({"type": "image_url", "image_url": {"url": uri}})

    request_payload = dict(payload)
    request_payload["model"] = model
    request_payload["messages"] = [
        {
            "role": "user",
            "content": content if len(content) > 1 else prompt_text,
        }
    ]
    request_payload["stream"] = False
    return await _normalize_openai_request(ChatCompletionRequest.model_validate(request_payload))


def _build_responses_image_payload(task: Dict[str, Any]) -> Dict[str, Any]:
    response_id = str(task.get("id") or "")
    status = str(task.get("status") or "queued")
    error_message = task.get("error")
    response_error = None
    output: List[Dict[str, Any]] = []

    if status == "failed":
        response_error = {
            "message": sanitize_public_error_message(error_message or "Image generation failed"),
            "type": "server_error",
            "code": "generation_failed",
        }
    elif status == "completed":
        item: Dict[str, Any] = {
            "id": f"ig_{response_id}_0",
            "type": "image_generation_call",
            "status": "completed",
            "result": task.get("b64_json"),
        }
        if task.get("url"):
            item["url"] = task["url"]
        output.append(item)

    return {
        "id": response_id,
        "object": "response",
        "created_at": int(task.get("created_at") or time.time()),
        "status": status,
        "error": response_error,
        "incomplete_details": None,
        "instructions": None,
        "metadata": {
            "task_id": response_id,
        },
        "model": task.get("model"),
        "output": output,
        "parallel_tool_calls": True,
        "temperature": None,
        "tool_choice": None,
        "tools": task.get("tools"),
        "top_p": None,
        "max_output_tokens": None,
        "previous_response_id": None,
        "reasoning": None,
        "text": None,
        "truncation": None,
        "usage": None,
        "user": None,
        "store": None,
    }


async def _update_image_response_task(response_id: str, **fields: Any) -> None:
    async with IMAGE_RESPONSE_TASKS_LOCK:
        task = IMAGE_RESPONSE_TASKS.get(response_id)
        if task is not None:
            task.update(fields)


async def _image_url_to_b64_json(image_url: str) -> Optional[str]:
    if image_url.startswith("data:image"):
        match = DATA_URL_RE.match(image_url)
        if match:
            return match.group("data")
        return None
    image_bytes = await retrieve_image_data(image_url)
    if not image_bytes:
        return None
    return base64.b64encode(image_bytes).decode("ascii")


async def _run_async_image_response_task(
    *,
    response_id: str,
    normalized: NormalizedGenerationRequest,
    response_format: str,
    base_url_override: Optional[str],
) -> None:
    await _update_image_response_task(response_id, status="in_progress")
    try:
        payload = _enrich_payload_with_direct_url(
            _parse_handler_result(
                await _collect_non_stream_result(
                    normalized.model,
                    normalized.prompt,
                    normalized.images,
                    base_url_override=base_url_override,
                    aspect_ratio_override=normalized.aspect_ratio_override,
                    watermark=normalized.watermark,
                )
            )
        )
        if "error" in payload:
            await _update_image_response_task(
                response_id,
                status="failed",
                error=_extract_error_message(payload),
                completed_at=int(time.time()),
            )
            return

        image_url = _extract_url_from_openai_payload(payload)
        if not image_url:
            await _update_image_response_task(
                response_id,
                status="failed",
                error="Image task completed without an image URL",
                completed_at=int(time.time()),
            )
            return

        b64_json = None
        if response_format == "b64_json":
            b64_json = await _image_url_to_b64_json(image_url)
            if not b64_json:
                await _update_image_response_task(
                    response_id,
                    status="failed",
                    error="Image task completed but base64 conversion failed",
                    completed_at=int(time.time()),
                )
                return

        await _update_image_response_task(
            response_id,
            status="completed",
            url=image_url,
            b64_json=b64_json,
            completed_at=int(time.time()),
        )
    except Exception as exc:
        debug_logger.log_error(f"[RESPONSES IMAGE] task failed id={response_id}: {exc}")
        await _update_image_response_task(
            response_id,
            status="failed",
            error=sanitize_public_error_message(str(exc) or exc.__class__.__name__),
            completed_at=int(time.time()),
        )


async def _create_async_image_response_task(
    normalized: NormalizedGenerationRequest,
    payload: Dict[str, Any],
    base_url_override: Optional[str],
) -> Dict[str, Any]:
    response_id = _new_image_response_id()
    task = {
        "id": response_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": "queued",
        "model": normalized.model,
        "tools": payload.get("tools"),
        "response_format": _responses_image_output_format(payload),
        "url": None,
        "b64_json": None,
        "error": None,
    }
    async with IMAGE_RESPONSE_TASKS_LOCK:
        IMAGE_RESPONSE_TASKS[response_id] = task
    _spawn_route_background_task(
        _run_async_image_response_task(
            response_id=response_id,
            normalized=normalized,
            response_format=task["response_format"],
            base_url_override=base_url_override,
        )
    )
    return _build_responses_image_payload(task)


def _build_gemini_error_payload(status_code: int, message: str) -> Dict[str, Any]:
    return {
        "error": {
            "code": status_code,
            "message": sanitize_public_error_message(message),
            "status": GEMINI_STATUS_MAP.get(status_code, "UNKNOWN"),
        }
    }


def _build_gemini_error_response_from_handler(payload: Dict[str, Any]) -> JSONResponse:
    error = payload.get("error", {})
    status_code = _get_error_status_code(payload)
    message = error.get("message", "Generation failed")
    return JSONResponse(
        status_code=status_code,
        content=_build_gemini_error_payload(status_code, message),
    )


def _extract_openai_message_content(payload: Dict[str, Any]) -> str:
    choices = payload.get("choices", [])
    if not choices:
        return payload.get("result", "")

    message = choices[0].get("message", {})
    content = message.get("content", "")
    return content if isinstance(content, str) else ""


def _extract_url_from_openai_payload(payload: Dict[str, Any]) -> Optional[str]:
    direct_url = payload.get("url")
    if isinstance(direct_url, str) and direct_url.strip():
        return direct_url.strip()

    content = _extract_openai_message_content(payload).strip()
    if not content:
        return None

    image_match = MARKDOWN_IMAGE_RE.search(content)
    if image_match:
        return image_match.group(1).strip()

    video_match = HTML_VIDEO_RE.search(content)
    if video_match:
        return video_match.group(1).strip()

    return None


def _enrich_payload_with_direct_url(payload: Dict[str, Any]) -> Dict[str, Any]:
    extracted_url = _extract_url_from_openai_payload(payload)
    if extracted_url and not payload.get("url"):
        payload["url"] = extracted_url
    return payload


async def _build_image_parts_from_uri(uri: str) -> List[Dict[str, Any]]:
    if uri.startswith("data:image"):
        mime_type, _ = _decode_data_url(uri)
        match = DATA_URL_RE.match(uri)
        if match:
            return [{"inlineData": {"mimeType": mime_type, "data": match.group("data")}}]

    image_bytes = await retrieve_image_data(uri)
    if image_bytes:
        mime_type = _detect_image_mime_type(
            image_bytes,
            fallback=_guess_mime_type(uri, "image/png"),
        )
        return [
            {
                "inlineData": {
                    "mimeType": mime_type,
                    "data": base64.b64encode(image_bytes).decode("ascii"),
                }
            }
        ]

    return [
        {
            "fileData": {
                "mimeType": _guess_mime_type(uri, "image/png"),
                "fileUri": uri,
            }
        },
        {"text": uri},
    ]


def _build_video_parts_from_uri(uri: str) -> List[Dict[str, Any]]:
    return [
        {
            "fileData": {
                "mimeType": _guess_mime_type(uri, "video/mp4"),
                "fileUri": uri,
            }
        }
    ]


async def _build_gemini_parts_from_output(output: str) -> List[Dict[str, Any]]:
    if not output:
        return []

    image_matches = MARKDOWN_IMAGE_RE.findall(output)
    if image_matches:
        parts: List[Dict[str, Any]] = []
        for uri in image_matches:
            parts.extend(await _build_image_parts_from_uri(uri))
        return parts

    video_matches = HTML_VIDEO_RE.findall(output)
    if video_matches:
        parts: List[Dict[str, Any]] = []
        for uri in video_matches:
            parts.extend(_build_video_parts_from_uri(uri))
        return parts

    return [{"text": output}]


async def _build_gemini_success_payload(
    payload: Dict[str, Any],
    response_model: str,
) -> Dict[str, Any]:
    output = _extract_openai_message_content(payload)
    return {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": await _build_gemini_parts_from_output(output),
                },
                "finishReason": "STOP",
                "index": 0,
            }
        ],
        "modelVersion": response_model,
    }


def _normalize_finish_reason(reason: Optional[str]) -> Optional[str]:
    if reason is None:
        return None
    mapping = {
        "stop": "STOP",
        "length": "MAX_TOKENS",
        "content_filter": "SAFETY",
    }
    return mapping.get(reason, "STOP")


async def _convert_openai_stream_chunk_to_gemini_event(
    payload: Dict[str, Any],
    response_model: str,
) -> Optional[str]:
    choices = payload.get("choices", [])
    if not choices:
        return None

    choice = choices[0]
    delta = choice.get("delta", {})
    text = delta.get("reasoning_content") or delta.get("content") or ""
    finish_reason = _normalize_finish_reason(choice.get("finish_reason"))

    candidate: Dict[str, Any] = {"index": choice.get("index", 0)}
    if text:
        candidate["content"] = {
            "role": "model",
            "parts": await _build_gemini_parts_from_output(text),
        }
    if finish_reason:
        candidate["finishReason"] = finish_reason

    if len(candidate) == 1:
        return None

    chunk = {
        "candidates": [candidate],
        "modelVersion": response_model,
    }
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"


async def _iterate_openai_stream(
    normalized: NormalizedGenerationRequest,
    base_url_override: Optional[str] = None,
):
    handler = _ensure_generation_handler()
    async for chunk in handler.handle_generation(
        model=normalized.model,
        prompt=normalized.prompt,
        images=normalized.images if normalized.images else None,
        stream=True,
        base_url_override=base_url_override,
        video_media_id=normalized.video_media_id,
        video_bytes=normalized.video_bytes,
        video_mime_type=normalized.video_mime_type,
        video_file_name=normalized.video_file_name,
        aspect_ratio_override=normalized.aspect_ratio_override,
        watermark=normalized.watermark,
    ):
        if chunk.startswith("data: "):
            yield chunk
            continue

        payload = _parse_handler_result(chunk)
        yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    yield "data: [DONE]\n\n"


async def _iterate_gemini_stream(
    normalized: NormalizedGenerationRequest,
    response_model: str,
    base_url_override: Optional[str] = None,
):
    handler = _ensure_generation_handler()
    async for chunk in handler.handle_generation(
        model=normalized.model,
        prompt=normalized.prompt,
        images=normalized.images if normalized.images else None,
        stream=True,
        base_url_override=base_url_override,
        video_media_id=normalized.video_media_id,
        video_bytes=normalized.video_bytes,
        video_mime_type=normalized.video_mime_type,
        video_file_name=normalized.video_file_name,
        aspect_ratio_override=normalized.aspect_ratio_override,
        watermark=normalized.watermark,
    ):
        if chunk.startswith("data: "):
            payload_text = chunk[6:].strip()
            if payload_text == "[DONE]":
                continue
            payload = _parse_handler_result(payload_text)
            if "error" in payload:
                yield (
                    f"data: {json.dumps(_build_gemini_error_payload(_get_error_status_code(payload), payload['error'].get('message', 'Generation failed')), ensure_ascii=False)}\n\n"
                )
                return

            event = await _convert_openai_stream_chunk_to_gemini_event(
                payload,
                response_model,
            )
            if event:
                yield event
            continue

        payload = _parse_handler_result(chunk)
        if "error" in payload:
            yield (
                f"data: {json.dumps(_build_gemini_error_payload(_get_error_status_code(payload), payload['error'].get('message', 'Generation failed')), ensure_ascii=False)}\n\n"
            )
            return

        event = await _convert_openai_stream_chunk_to_gemini_event(
            payload,
            response_model,
        )
        if event:
            yield event


@router.get("/v1/models")
async def list_models(api_key: str = Depends(verify_api_key_flexible)):
    """List available models."""
    models = [
        {
            "id": model["id"],
            "object": "model",
            "owned_by": "flow2api",
            "description": model["description"],
        }
        for model in _get_openai_model_catalog()
    ]

    return {"object": "list", "data": models}


@router.get("/v1/models/aliases")
async def list_model_aliases(api_key: str = Depends(verify_api_key_flexible)):
    """List simplified model aliases for generationConfig-based resolution."""
    aliases = get_base_model_aliases()
    alias_models = []
    for alias_id, description in aliases.items():
        alias_models.append(
            {
                "id": alias_id,
                "object": "model",
                "owned_by": "flow2api",
                "description": description,
                "is_alias": True,
            }
        )
    return {"object": "list", "data": alias_models}


@router.get("/v1beta/models")
@router.get("/models")
async def list_gemini_models(api_key: str = Depends(verify_api_key_flexible)):
    """List available models using Gemini-compatible response shape."""
    catalog = _get_gemini_model_catalog()
    return {
        "models": [
            _build_gemini_model_resource(model_id, description)
            for model_id, description in catalog.items()
        ]
    }


@router.get("/v1beta/models/{model}")
@router.get("/models/{model}")
async def get_gemini_model(model: str, api_key: str = Depends(verify_api_key_flexible)):
    """Return a single model using Gemini-compatible response shape."""
    catalog = _get_gemini_model_catalog()
    description = catalog.get(model)
    if not description:
        return JSONResponse(
            status_code=404,
            content=_build_gemini_error_payload(404, f"Model not found: {model}"),
        )

    return _build_gemini_model_resource(model, description)


@router.post("/v1/chat/completions")
async def create_chat_completion(
    request: ChatCompletionRequest,
    raw_request: Request,
    api_key: str = Depends(verify_api_key_flexible),
):
    """OpenAI-compatible unified generation endpoint."""
    try:
        normalized = await _normalize_openai_request(request)
        if not normalized.prompt:
            raise HTTPException(status_code=400, detail="Prompt cannot be empty")

        request_base_url = _get_request_base_url(raw_request)

        if request.stream:
            return StreamingResponse(
                _iterate_openai_stream(normalized, request_base_url),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

        payload = _parse_handler_result(
            await _collect_non_stream_result(
                normalized.model,
                normalized.prompt,
                normalized.images,
                base_url_override=request_base_url,
                video_media_id=normalized.video_media_id,
                video_bytes=normalized.video_bytes,
                video_mime_type=normalized.video_mime_type,
                video_file_name=normalized.video_file_name,
                aspect_ratio_override=normalized.aspect_ratio_override,
                watermark=normalized.watermark,
            )
        )
        return _build_openai_json_response(payload)

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=sanitize_public_error_message(exc))


@router.post("/v1/responses")
async def create_image_response(
    raw_request: Request,
    api_key: str = Depends(verify_api_key_flexible),
):
    try:
        payload = await _read_request_payload(raw_request)
        normalized = await _normalize_responses_image_request(payload)
        if MODEL_CONFIG.get(normalized.model, {}).get("type") != "image":
            raise HTTPException(status_code=400, detail=f"Model is not an image model: {normalized.model}")
        result = await _create_async_image_response_task(
            normalized=normalized,
            payload=payload,
            base_url_override=_get_request_base_url(raw_request),
        )
        return JSONResponse(content=result)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=sanitize_public_error_message(exc))


@router.get("/v1/responses/{response_id:path}")
async def get_image_response(
    response_id: str,
    api_key: str = Depends(verify_api_key_flexible),
):
    response_id = unquote(response_id)
    async with IMAGE_RESPONSE_TASKS_LOCK:
        task = dict(IMAGE_RESPONSE_TASKS.get(response_id) or {})
    if not task:
        raise HTTPException(status_code=404, detail=f"Response not found: {response_id}")
    return JSONResponse(content=_build_responses_image_payload(task))


@router.post("/v1/videos")
@router.post("/api/v3/contents/generations/tasks")
async def create_video_task(
    raw_request: Request,
    api_key: str = Depends(verify_api_key_flexible),
):
    try:
        payload = await _read_request_payload(raw_request)
        normalized = await _normalize_video_create_payload(payload)
        if MODEL_CONFIG.get(normalized.model, {}).get("type") != "video":
            raise HTTPException(status_code=400, detail=f"Model is not a video model: {normalized.model}")
        validation_error = _video_task_validation_error(normalized)
        if validation_error:
            return _build_openai_json_response(validation_error)

        result = await _create_deferred_async_video_task(
            normalized,
            _get_request_base_url(raw_request),
        )
        if "error" in result:
            return _build_openai_json_response(result)

        if raw_request.url.path.startswith("/api/v3/contents/generations/tasks"):
            return JSONResponse(content=_format_seedance_task_payload(result))
        return JSONResponse(content=result)

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=sanitize_public_error_message(exc))


@router.post(
    "/v1/videos/remove-watermark",
    response_model=WatermarkRemovalResponse,
    responses={
        400: {"description": "Invalid or unsupported video URL"},
        502: {"description": "Watermark processing failed"},
        503: {"description": "Watermark service is not configured"},
    },
)
async def remove_video_watermark(
    request: WatermarkRemovalRequest,
    raw_request: Request,
    api_key: str = Depends(verify_api_key_flexible),
):
    """Remove a Flow video watermark and return the uploaded output URL."""
    handler = _ensure_generation_handler()
    try:
        output_url = await handler.watermark_processor.remove_watermark(
            url=request.source_url,
            file_cache=handler.file_cache,
            public_base_url=_get_request_base_url(raw_request),
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=sanitize_public_error_message(exc),
        ) from exc
    except RuntimeError as exc:
        debug_logger.log_error(
            error_message=f"Public watermark removal failed: {exc}",
            status_code=0,
            response_text="",
        )
        message = str(exc).lower()
        if (
            "s3 upload is disabled" in message
            or "s3 upload config missing" in message
            or "gwt-video not found" in message
        ):
            raise HTTPException(
                status_code=503,
                detail="Watermark removal service is not configured",
            ) from exc
        raise HTTPException(status_code=502, detail="Watermark removal failed") from exc
    except Exception as exc:
        debug_logger.log_error(
            error_message=f"Public watermark removal failed: {exc}",
            status_code=0,
            response_text="",
        )
        raise HTTPException(status_code=502, detail="Watermark removal failed") from exc

    return {
        "id": f"wmr_{uuid.uuid4().hex}",
        "object": "video.watermark_removal",
        "status": "completed",
        "source_url": request.source_url,
        "video_url": output_url,
    }


@router.get("/v1/videos/{task_id:path}")
@router.get("/api/v3/contents/generations/tasks/{task_id:path}")
async def get_video_task(
    task_id: str,
    raw_request: Request,
    api_key: str = Depends(verify_api_key_flexible),
):
    handler = _ensure_generation_handler()
    payload = await handler.get_video_task_payload(unquote(task_id))
    if payload is None:
        raise HTTPException(status_code=404, detail=f"Video task not found: {task_id}")

    if raw_request.url.path.startswith("/api/v3/contents/generations/tasks"):
        return JSONResponse(content=_format_seedance_task_payload(payload))
    return JSONResponse(content=payload)


@router.post("/v1beta/models/{model}:generateContent")
@router.post("/models/{model}:generateContent")
async def generate_content(
    model: str,
    request: GeminiGenerateContentRequest,
    raw_request: Request,
    api_key: str = Depends(verify_api_key_flexible),
):
    """Gemini official generateContent endpoint."""
    try:
        normalized = await _normalize_gemini_request(model, request)
        if not normalized.prompt:
            raise HTTPException(status_code=400, detail="Prompt cannot be empty")

        request_base_url = _get_request_base_url(raw_request)

        payload = _enrich_payload_with_direct_url(
            _parse_handler_result(
                await _collect_non_stream_result(
                    normalized.model,
                    normalized.prompt,
                    normalized.images,
                    base_url_override=request_base_url,
                    video_media_id=normalized.video_media_id,
                    video_bytes=normalized.video_bytes,
                    video_mime_type=normalized.video_mime_type,
                    video_file_name=normalized.video_file_name,
                    aspect_ratio_override=normalized.aspect_ratio_override,
                    watermark=normalized.watermark,
                )
            )
        )
        if "error" in payload:
            return _build_gemini_error_response_from_handler(payload)

        return JSONResponse(
            content=await _build_gemini_success_payload(payload, normalized.model)
        )

    except HTTPException as exc:
        return JSONResponse(
            status_code=exc.status_code,
            content=_build_gemini_error_payload(exc.status_code, str(exc.detail)),
        )
    except Exception as exc:
        return JSONResponse(
            status_code=500,
            content=_build_gemini_error_payload(500, str(exc)),
        )


@router.post("/v1beta/models/{model}:streamGenerateContent")
@router.post("/models/{model}:streamGenerateContent")
async def stream_generate_content(
    model: str,
    request: GeminiGenerateContentRequest,
    raw_request: Request,
    alt: Optional[str] = Query(None),
    api_key: str = Depends(verify_api_key_flexible),
):
    """Gemini official streamGenerateContent endpoint."""
    try:
        normalized = await _normalize_gemini_request(model, request)
        if not normalized.prompt:
            raise HTTPException(status_code=400, detail="Prompt cannot be empty")

        request_base_url = _get_request_base_url(raw_request)

        return StreamingResponse(
            _iterate_gemini_stream(normalized, normalized.model, request_base_url),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
    except HTTPException as exc:
        return JSONResponse(
            status_code=exc.status_code,
            content=_build_gemini_error_payload(exc.status_code, str(exc.detail)),
        )
    except Exception as exc:
        return JSONResponse(
            status_code=500,
            content=_build_gemini_error_payload(500, str(exc)),
        )

@router.websocket("/captcha_ws")
async def captcha_websocket_endpoint(websocket: WebSocket):
    from ..core.logger import debug_logger
    api_key = (
        websocket.query_params.get("key")
        or websocket.query_params.get("api_key")
        or websocket.headers.get("x-goog-api-key")
        or ""
    ).strip()
    authorization = (websocket.headers.get("authorization") or "").strip()
    if authorization.lower().startswith("bearer "):
        api_key = authorization[7:].strip()

    if not api_key or not AuthManager.verify_api_key(api_key):
        await websocket.close(code=1008)
        return

    service = await ExtensionCaptchaService.get_instance()
    await service.connect(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            await service.handle_message(websocket, data)
    except WebSocketDisconnect:
        service.disconnect(websocket)
    except Exception as e:
        debug_logger.log_error(f"WebSocket error: {e}")
        service.disconnect(websocket)
