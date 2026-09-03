"""Shared media-generation error classification and public-message helpers."""

import re
from typing import Any, Optional


MEDIA_TRAFFIC_ERROR_KEYWORDS = (
    "public_error_unusual_activity_too_much_traffic",
    "public_error_unusual_activity",
    "too many requests",
    "http error 429",
)

MEDIA_POLICY_REASON_UNSAFE_GENERATION = "unsafe_generation"
MEDIA_POLICY_REASON_PROMINENT_PEOPLE = "prominent_people_filter"
MEDIA_TRAFFIC_REASON = "upstream_traffic_control"
MEDIA_INVALID_ARGUMENT_REASON = "invalid_argument"
MEDIA_TRANSPORT_REASON = "transport_error"

MEDIA_INVALID_ARGUMENT_FAILURE_MESSAGE = (
    "\u63d0\u793a\u8bcd\u6216\u53c2\u8003\u7d20\u6750\u88ab\u62d2\u7edd\uff0c"
    "\u8bf7\u8c03\u6574\u63d0\u793a\u8bcd\u6216\u53c2\u8003\u7d20\u6750\u540e\u91cd\u8bd5"
)
MEDIA_TRANSPORT_FAILURE_MESSAGE = (
    "\u751f\u6210\u670d\u52a1\u7f51\u7edc\u8fde\u63a5\u5f02\u5e38\uff0c\u8bf7\u7a0d\u540e\u91cd\u8bd5"
)

IMAGE_POLICY_FAILURE_MESSAGE = "图片生成被内容安全策略拒绝，请调整提示词或参考图后重试"
VIDEO_POLICY_FAILURE_MESSAGE = "视频生成被内容安全策略拒绝，请调整提示词或参考图后重试"
MEDIA_POLICY_FAILURE_MESSAGE = "媒体生成被内容安全策略拒绝，请调整提示词或参考素材后重试"
IMAGE_PROMINENT_PEOPLE_FAILURE_MESSAGE = (
    "图片生成触发人物/公众人物过滤，请更换参考图，或移除严格锁脸、身份复刻类描述后重试"
)
VIDEO_PROMINENT_PEOPLE_FAILURE_MESSAGE = (
    "视频生成触发人物/公众人物过滤，请更换参考图，或移除严格锁脸、身份复刻类描述后重试"
)
MEDIA_PROMINENT_PEOPLE_FAILURE_MESSAGE = (
    "媒体生成触发人物/公众人物过滤，请更换参考素材，或移除严格锁脸、身份复刻类描述后重试"
)
UPSTREAM_TRAFFIC_FAILURE_MESSAGE = (
    "媒体生成服务当前请求较多，请稍后重试"
)
IMAGE_SERVICE_UNAVAILABLE_MESSAGE = "图片生成服务暂时不可用，请稍后重试"
VIDEO_SERVICE_UNAVAILABLE_MESSAGE = "视频生成服务暂时不可用，请稍后重试"
MEDIA_SERVICE_UNAVAILABLE_MESSAGE = "媒体生成服务暂时不可用，请稍后重试"
VIDEO_UPLOAD_INVALID_ARGUMENT_MESSAGE = (
    "参考图上传请求被拒绝，请更换或重新压缩参考图后重试；"
    "如仍失败，请重新创建该账号的生成项目"
)
VIDEO_UPLOAD_FAILURE_MESSAGE = (
    "参考图上传失败，请稍后重试；如持续失败，请重新创建该账号的生成项目"
)


_PUBLIC_UPSTREAM_PATTERNS = (
    (
        re.compile(
            r"https?://[^\s\]\[(){}<>\"']*flow[^\s\]\[(){}<>\"']*",
            re.IGNORECASE,
        ),
        "生成服务地址",
    ),
    (
        re.compile(r"/flow/[a-z0-9._~!$&'()*+,;=:@%/-]*", re.IGNORECASE),
        "生成服务接口",
    ),
    (re.compile(r"\bflow\s*2\s*api\b", re.IGNORECASE), "生成服务"),
    (re.compile(r"\bflow(?:\s+browser)?\s+api\b", re.IGNORECASE), "生成服务"),
    (re.compile(r"\bflow\b", re.IGNORECASE), "生成服务"),
    (re.compile(r"\bupstream\b", re.IGNORECASE), "生成服务"),
)


def sanitize_public_error_message(error_message: Any) -> str:
    """Remove provider names, endpoints, and upstream terminology from public errors."""
    text = str(error_message or "").strip() or "生成服务暂时不可用，请稍后重试"
    text = text.replace("上游", "生成服务")
    for pattern, replacement in _PUBLIC_UPSTREAM_PATTERNS:
        text = pattern.sub(replacement, text)
    return re.sub(r"(?:生成服务\s*){2,}", "生成服务", text).strip()


def media_service_unavailable_message(media_type: str) -> str:
    """Return a public availability message without exposing account routing."""
    if media_type == "image":
        return IMAGE_SERVICE_UNAVAILABLE_MESSAGE
    if media_type == "video":
        return VIDEO_SERVICE_UNAVAILABLE_MESSAGE
    return MEDIA_SERVICE_UNAVAILABLE_MESSAGE


def media_policy_reason(error_message: Any) -> Optional[str]:
    """Return the stable reason for an upstream content-policy rejection."""
    error_lower = str(error_message or "").strip().lower()
    if (
        "public_error_prominent_people_filter_failed" in error_lower
        or "prominent_people_filter_failed" in error_lower
    ):
        return MEDIA_POLICY_REASON_PROMINENT_PEOPLE
    if (
        "public_error_unsafe_generation" in error_lower
        or "unsafe_generation" in error_lower
    ):
        return MEDIA_POLICY_REASON_UNSAFE_GENERATION
    return None


def is_media_policy_error(error_message: Any) -> bool:
    """Return True for upstream content-safety/policy rejections."""
    return media_policy_reason(error_message) is not None


def is_media_traffic_error(error_message: Any) -> bool:
    """Return True for upstream rate/abnormal-activity controls."""
    error_lower = str(error_message or "").strip().lower()
    return any(keyword in error_lower for keyword in MEDIA_TRAFFIC_ERROR_KEYWORDS)


def is_media_invalid_argument_error(error_message: Any) -> bool:
    """Return True for a generic generation-submit invalid-argument rejection."""
    error_lower = str(error_message or "").strip().lower()
    return "request contains an invalid argument" in error_lower


def is_media_transport_error(error_message: Any) -> bool:
    """Return True for a media request that failed in the HTTP transport layer."""
    error_lower = str(error_message or "").strip().lower()
    return (
        "curl: (16)" in error_lower
        or "curle_http2" in error_lower
        or "error in the http2 framing layer" in error_lower
    )


def media_generation_failure_reason(error_message: Any) -> Optional[str]:
    """Return a stable diagnostic reason without exposing the upstream message."""
    policy_reason = media_policy_reason(error_message)
    if policy_reason:
        return policy_reason
    if is_media_traffic_error(error_message):
        return MEDIA_TRAFFIC_REASON
    if is_media_invalid_argument_error(error_message):
        return MEDIA_INVALID_ARGUMENT_REASON
    if is_media_transport_error(error_message):
        return MEDIA_TRANSPORT_REASON
    return None


def is_project_image_upload_invalid_argument_error(error_message: Any) -> bool:
    """Return True for project-scoped reference-image upload 400s."""
    error_lower = str(error_message or "").strip().lower()
    return (
        "project-scoped image upload failed via /flow/uploadimage" in error_lower
        and "request contains an invalid argument" in error_lower
    )


def is_project_image_upload_error(error_message: Any) -> bool:
    """Return True for any project-scoped reference-image upload failure."""
    error_lower = str(error_message or "").strip().lower()
    return "project-scoped image upload failed via /flow/uploadimage" in error_lower


def project_image_upload_failure_response(error_message: Any) -> tuple[str, int]:
    """Return a safe public response for project-scoped upload failures."""
    if is_project_image_upload_invalid_argument_error(error_message):
        return VIDEO_UPLOAD_INVALID_ARGUMENT_MESSAGE, 400
    return VIDEO_UPLOAD_FAILURE_MESSAGE, 502


def media_policy_failure_message(media_type: str, error_message: Any = None) -> str:
    if media_policy_reason(error_message) == MEDIA_POLICY_REASON_PROMINENT_PEOPLE:
        if media_type == "video":
            return VIDEO_PROMINENT_PEOPLE_FAILURE_MESSAGE
        if media_type == "image":
            return IMAGE_PROMINENT_PEOPLE_FAILURE_MESSAGE
        return MEDIA_PROMINENT_PEOPLE_FAILURE_MESSAGE
    if media_type == "video":
        return VIDEO_POLICY_FAILURE_MESSAGE
    if media_type == "image":
        return IMAGE_POLICY_FAILURE_MESSAGE
    return MEDIA_POLICY_FAILURE_MESSAGE


def media_generation_failure_response(
    media_type: str,
    error_message: Any,
) -> tuple[str, int]:
    text = str(error_message or "").strip() or "\u672a\u77e5\u9519\u8bef"
    if is_media_policy_error(text):
        return media_policy_failure_message(media_type, text), 400
    if is_media_traffic_error(text):
        media_label = {
            "image": "\u56fe\u7247",
            "video": "\u89c6\u9891",
        }.get(media_type, "\u5a92\u4f53")
        return (
            UPSTREAM_TRAFFIC_FAILURE_MESSAGE.replace("\u5a92\u4f53", media_label, 1),
            429,
        )
    if is_media_invalid_argument_error(text):
        return MEDIA_INVALID_ARGUMENT_FAILURE_MESSAGE, 400
    if is_media_transport_error(text):
        return MEDIA_TRANSPORT_FAILURE_MESSAGE, 502
    media_label = {
        "image": "\u56fe\u7247",
        "video": "\u89c6\u9891",
    }.get(media_type, "\u5a92\u4f53")
    return (
        f"{media_label}\u751f\u6210\u5931\u8d25: {sanitize_public_error_message(text)}\uff0c"
        "\u8bf7\u91cd\u8bd5",
        502,
    )
