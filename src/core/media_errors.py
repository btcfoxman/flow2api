"""Shared media-generation error classification helpers."""

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

IMAGE_POLICY_FAILURE_MESSAGE = (
    "\u56fe\u7247\u751f\u6210\u88ab\u4e0a\u6e38\u5185\u5bb9"
    "\u5b89\u5168\u7b56\u7565\u62d2\u7edd\uff0c\u8bf7\u8c03"
    "\u6574\u63d0\u793a\u8bcd\u6216\u53c2\u8003\u56fe\u540e"
    "\u91cd\u8bd5"
)
VIDEO_POLICY_FAILURE_MESSAGE = (
    "\u89c6\u9891\u751f\u6210\u88ab\u4e0a\u6e38\u5185\u5bb9"
    "\u5b89\u5168\u7b56\u7565\u62d2\u7edd\uff0c\u8bf7\u8c03"
    "\u6574\u63d0\u793a\u8bcd\u6216\u53c2\u8003\u56fe\u540e"
    "\u91cd\u8bd5"
)
MEDIA_POLICY_FAILURE_MESSAGE = (
    "\u5a92\u4f53\u751f\u6210\u88ab\u4e0a\u6e38\u5185\u5bb9"
    "\u5b89\u5168\u7b56\u7565\u62d2\u7edd\uff0c\u8bf7\u8c03"
    "\u6574\u63d0\u793a\u8bcd\u6216\u53c2\u8003\u7d20\u6750"
    "\u540e\u91cd\u8bd5"
)
IMAGE_PROMINENT_PEOPLE_FAILURE_MESSAGE = (
    "\u56fe\u7247\u751f\u6210\u89e6\u53d1\u4e0a\u6e38\u4eba"
    "\u7269/\u516c\u4f17\u4eba\u7269\u8fc7\u6ee4\uff0c\u8bf7"
    "\u66f4\u6362\u53c2\u8003\u56fe\uff0c\u6216\u79fb\u9664"
    "\u4e25\u683c\u9501\u8138\u3001\u8eab\u4efd\u590d\u523b"
    "\u7c7b\u63cf\u8ff0\u540e\u91cd\u8bd5"
)
VIDEO_PROMINENT_PEOPLE_FAILURE_MESSAGE = (
    "\u89c6\u9891\u751f\u6210\u89e6\u53d1\u4e0a\u6e38\u4eba"
    "\u7269/\u516c\u4f17\u4eba\u7269\u8fc7\u6ee4\uff0c\u8bf7"
    "\u66f4\u6362\u53c2\u8003\u56fe\uff0c\u6216\u79fb\u9664"
    "\u4e25\u683c\u9501\u8138\u3001\u8eab\u4efd\u590d\u523b"
    "\u7c7b\u63cf\u8ff0\u540e\u91cd\u8bd5"
)
MEDIA_PROMINENT_PEOPLE_FAILURE_MESSAGE = (
    "\u5a92\u4f53\u751f\u6210\u89e6\u53d1\u4e0a\u6e38\u4eba"
    "\u7269/\u516c\u4f17\u4eba\u7269\u8fc7\u6ee4\uff0c\u8bf7"
    "\u66f4\u6362\u53c2\u8003\u7d20\u6750\uff0c\u6216\u79fb"
    "\u9664\u4e25\u683c\u9501\u8138\u3001\u8eab\u4efd\u590d"
    "\u523b\u7c7b\u63cf\u8ff0\u540e\u91cd\u8bd5"
)
UPSTREAM_TRAFFIC_FAILURE_MESSAGE = (
    "\u5a92\u4f53\u751f\u6210\u89e6\u53d1\u4e0a\u6e38\u6d41"
    "\u91cf\u6216\u5f02\u5e38\u6d3b\u52a8\u98ce\u63a7\uff0c"
    "\u8bf7\u7a0d\u540e\u91cd\u8bd5\uff1b\u8fd9\u4e0d\u662f"
    "\u5185\u5bb9\u5b89\u5168\u62d2\u7edd\uff0c\u65e0\u9700"
    "\u4fee\u6539\u63d0\u793a\u8bcd\u6216\u53c2\u8003\u56fe"
)
VIDEO_UPLOAD_INVALID_ARGUMENT_MESSAGE = (
    "\u53c2\u8003\u56fe\u4e0a\u4f20\u88ab\u4e0a\u6e38\u62d2"
    "\u7edd\uff0c\u8bf7\u66f4\u6362\u6216\u91cd\u65b0\u538b"
    "\u7f29\u53c2\u8003\u56fe\u540e\u91cd\u8bd5"
)


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


def media_generation_failure_reason(error_message: Any) -> Optional[str]:
    """Return a stable diagnostic reason without exposing the upstream message."""
    policy_reason = media_policy_reason(error_message)
    if policy_reason:
        return policy_reason
    if is_media_traffic_error(error_message):
        return MEDIA_TRAFFIC_REASON
    return None


def is_project_image_upload_invalid_argument_error(error_message: Any) -> bool:
    """Return True for project-scoped reference-image upload 400s."""
    error_lower = str(error_message or "").strip().lower()
    return (
        "project-scoped image upload failed via /flow/uploadimage" in error_lower
        and "request contains an invalid argument" in error_lower
    )


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
    media_label = {
        "image": "\u56fe\u7247",
        "video": "\u89c6\u9891",
    }.get(media_type, "\u5a92\u4f53")
    return (
        f"{media_label}\u751f\u6210\u5931\u8d25: {text}\uff0c"
        "\u8bf7\u91cd\u8bd5",
        502,
    )
