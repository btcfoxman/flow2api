"""Shared media-generation error classification helpers."""

from typing import Any


MEDIA_POLICY_ERROR_KEYWORDS = (
    "public_error_unsafe_generation",
    "unsafe_generation",
    "public_error_prominent_people_filter_failed",
    "prominent_people_filter_failed",
)

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
VIDEO_UPLOAD_INVALID_ARGUMENT_MESSAGE = (
    "\u53c2\u8003\u56fe\u4e0a\u4f20\u88ab\u4e0a\u6e38\u62d2"
    "\u7edd\uff0c\u8bf7\u66f4\u6362\u6216\u91cd\u65b0\u538b"
    "\u7f29\u53c2\u8003\u56fe\u540e\u91cd\u8bd5"
)


def is_media_policy_error(error_message: Any) -> bool:
    """Return True for upstream content-safety/policy rejections."""
    error_lower = str(error_message or "").strip().lower()
    return any(keyword in error_lower for keyword in MEDIA_POLICY_ERROR_KEYWORDS)


def is_project_image_upload_invalid_argument_error(error_message: Any) -> bool:
    """Return True for project-scoped reference-image upload 400s."""
    error_lower = str(error_message or "").strip().lower()
    return (
        "project-scoped image upload failed via /flow/uploadimage" in error_lower
        and "request contains an invalid argument" in error_lower
    )


def media_policy_failure_message(media_type: str) -> str:
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
        return media_policy_failure_message(media_type), 400
    media_label = {
        "image": "\u56fe\u7247",
        "video": "\u89c6\u9891",
    }.get(media_type, "\u5a92\u4f53")
    return (
        f"{media_label}\u751f\u6210\u5931\u8d25: {text}\uff0c"
        "\u8bf7\u91cd\u8bd5",
        502,
    )
