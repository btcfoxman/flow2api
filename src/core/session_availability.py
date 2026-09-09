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
                  for key in ("st", "google_cookies", "captcha_proxy_url")]
        return hashlib.sha256(json.dumps(values).encode()).digest()

    def reject(self, token):
        self._blocked[int(token.id)] = (self._revision(token), time.monotonic() + self.RETRY_SECONDS)

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
