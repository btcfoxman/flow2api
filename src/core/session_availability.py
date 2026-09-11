"""Account-local, credential-revision-aware backoff for rejected browser sessions."""
import hashlib
import json
import time


class SessionAvailability:
    # A bounded probe interval also allows an operator's in-browser login to recover
    # without an imported credential change. This is not an account or proxy ban.
    RETRY_SECONDS = 300

    def __init__(self):
        self._blocked = {}

    @staticmethod
    def _revision(token):
        # AT refresh and balance changes do not repair a rejected Google session.
        values = [str(getattr(token, key, None) or "")
                  for key in ("auth_mode", "st", "google_cookies", "captcha_proxy_url")]
        return hashlib.sha256(json.dumps(values).encode()).digest()

    def reject(self, token, error=None):
        # A confirmed signed-out imported session is not a transient transport
        # failure. Retrying it with user jobs every five minutes cannot repair it.
        # Explicit sync verification can still probe without submitting media.
        from .generation_errors import NativeSessionError
        from .native_session_state import local_session_state
        if isinstance(error, NativeSessionError) and error.reason in {
            "flow_model_transport_unavailable", "flow_image_upload_not_verified",
            "flow_upsample_transport_unavailable", "flow_project_required",
            "flow_native_transport_required", "flow_browser_context_unavailable", "flow_browser_account_mismatch",
            "flow_legacy_transport_forbidden",
        }:
            # A missing adapter/project is not evidence of a rejected login.
            return
        signed_out = isinstance(error, NativeSessionError) and (
            error.reason in {"google_session_cookies_incomplete", "flow_login_unavailable", "flow_identity_mismatch", "flow_identity_unavailable", "upstream_authentication_rejected"}
            or (error.reason == "project_context_unavailable" and error.page_path.rstrip("/") == "/about")
        )
        try:
            locally_owned = bool(local_session_state(token.id)) if signed_out else False
        except NativeSessionError:
            locally_owned = False
        until = float("inf") if signed_out and not locally_owned else time.monotonic() + self.RETRY_SECONDS
        self._blocked[int(token.id)] = (self._revision(token), until)

    def available(self, token):
        entry = self._blocked.get(int(token.id))
        if entry is None:
            return True
        revision, until = entry
        if time.monotonic() >= until or revision != self._revision(token):
            self.discard(token.id)
            return True
        return False

    def discard(self, token_id):
        self._blocked.pop(int(token_id), None)
