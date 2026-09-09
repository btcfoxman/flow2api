"""Credential-free classification of local browser/session preflight failures."""
import re
from urllib.parse import urlsplit


class NativeSessionError(RuntimeError):
    """A session preflight or authentication rejection, not a generation verdict."""

    def __init__(self, reason: str, *, protocol: str = "labs", page_url: str = "", stage: str = "browser_preflight"):
        self.reason = reason
        self.protocol = protocol
        self.stage = stage
        parsed = urlsplit(page_url)
        # Never retain query strings, fragments, user info, or page contents.
        self.page_origin = f"{parsed.scheme}://{parsed.hostname}" if parsed.hostname else ""
        self.page_path = parsed.path[:180]
        super().__init__(f"native_session_unavailable: {reason} ({protocol})")

    def diagnostic(self):
        return {"stage": self.stage, "reason": self.reason,
                "protocol": self.protocol, "page_origin": self.page_origin,
                "page_path": self.page_path}


def is_native_session_error(error) -> bool:
    return isinstance(error, NativeSessionError) or "native_session_unavailable:" in str(error).lower()


def is_upstream_authentication_error(error) -> bool:
    """Recognize explicit HTTP authentication rejection, not arbitrary '401' text."""
    if isinstance(error, NativeSessionError):
        return error.reason == "upstream_authentication_rejected"
    return bool(re.search(r"\bHTTP(?:\s+Error)?\s*[:=]?\s*401\b", str(error), re.I))
