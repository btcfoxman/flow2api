"""Credit thresholds and quota-error helpers."""

from typing import Any


MIN_GENERATION_CREDITS = 20


def normalize_credits(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def has_minimum_generation_credits(value: Any) -> bool:
    return normalize_credits(value) >= MIN_GENERATION_CREDITS


def is_quota_exhausted_error(error_message: Any) -> bool:
    error_lower = str(error_message or "").strip().lower()
    return any(
        marker in error_lower
        for marker in (
            "public_error_user_quota_reached",
            "resource has been exhausted",
            "check quota",
            "quota reached",
            "quota_exceeded",
        )
    )


def quota_exhausted_message() -> str:
    return (
        f"\u8d26\u53f7\u989d\u5ea6\u4e0d\u8db3\uff08\u5df2\u6392\u9664"
        f"\u989d\u5ea6\u4f4e\u4e8e {MIN_GENERATION_CREDITS} \u7684"
        "\u8d26\u53f7\uff09\uff0c\u8bf7\u8865\u5145\u989d\u5ea6"
        "\u6216\u542f\u7528\u5176\u4ed6\u53ef\u7528\u8d26\u53f7"
    )
