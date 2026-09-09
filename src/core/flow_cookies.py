"""Validate scoped Google credentials without flattening their host boundaries."""
import json
import math
import re
import time
from typing import Any

COOKIE_HOSTS = {"google.com", "www.google.com", "accounts.google.com", "flow.google.com"}


def normalize_google_cookies(raw: Any) -> str:
    if isinstance(raw, str):
        if len(raw.encode("utf-8")) > 300_000:
            raise ValueError("google_cookies exceeds 300 KB")
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            raise ValueError("google_cookies must be a scoped JSON cookie array") from None
    if not isinstance(raw, list) or not 1 <= len(raw) <= 256:
        raise ValueError("google_cookies must contain 1 to 256 cookies")
    merged = {}
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("Invalid Google cookie entry")
        name, value, domain = item.get("name"), item.get("value"), item.get("domain")
        if not isinstance(name, str) or not re.fullmatch(r"[^\s=;,\x00-\x1f\x7f]{1,256}", name):
            raise ValueError("Invalid Google cookie name")
        if not isinstance(value, str) or len(value) > 16384 or re.search(r"[\x00-\x1f\x7f;]", value):
            raise ValueError("Invalid Google cookie value")
        if not isinstance(domain, str) or domain.lower().lstrip(".") not in COOKIE_HOSTS or domain.startswith(".."):
            raise ValueError("Google cookie domain is outside the allowed hosts")
        domain = domain.lower()
        path = item.get("path", "/")
        if not isinstance(path, str) or not path.startswith("/") or len(path) > 2048 or re.search(r"[\x00-\x1f\x7f]", path):
            raise ValueError("Invalid Google cookie path")
        if item.get("partitionKey") or item.get("partitioned"):
            raise ValueError("Partitioned cookies cannot be imported as unpartitioned cookies")
        cookie = {"name": name, "value": value, "domain": domain, "path": path,
                  "secure": bool(item.get("secure", True)), "httpOnly": bool(item.get("httpOnly", False))}
        same_site = {"lax": "Lax", "strict": "Strict", "none": "None", "no_restriction": "None"}.get(str(item.get("sameSite", "")).lower())
        if same_site:
            cookie["sameSite"] = same_site
        if name.startswith(("__Secure-", "__Host-")):
            cookie["secure"] = True
        if name.startswith("__Host-") and (domain.startswith(".") or path != "/"):
            raise ValueError("Invalid __Host- cookie scope")
        expires = item.get("expires", item.get("expirationDate"))
        if expires is not None:
            try:
                expiry = float(expires)
            except (ValueError, TypeError):
                raise ValueError("Invalid Google cookie expiry") from None
            if not math.isfinite(expiry):
                raise ValueError("Invalid Google cookie expiry")
            if expiry > 0:
                if expiry <= time.time():
                    continue
                cookie["expires"] = expiry
        merged[(name, domain, path)] = cookie
    if not merged:
        raise ValueError("No unexpired Google cookies supplied")
    result = json.dumps([merged[key] for key in sorted(merged)], separators=(",", ":"), ensure_ascii=True)
    if len(result.encode("utf-8")) > 300_000:
        raise ValueError("google_cookies exceeds 300 KB")
    return result


def google_cookie_status(raw: Any) -> dict:
    try:
        cookies = json.loads(normalize_google_cookies(raw))
    except (ValueError, TypeError):
        cookies = []
    return {
        "cookies_configured": bool(cookies),
        "flow_cookies_configured": any(c["domain"].lstrip(".") == "flow.google.com" and c["name"] in {"OSID", "__Secure-OSID"} for c in cookies),
        # HTTPS-only extension permissions expose Secure PSID but silently omit
        # SID. That partial snapshot passes OAuth yet redirects Flow to /about.
        "google_session_cookies_configured": any(c["domain"] == ".google.com" and c["path"] == "/" and c["name"] == "SID" and c["value"] for c in cookies),
        "cookie_count": len(cookies),
    }


def has_complete_flow_cookies(raw: Any) -> bool:
    status = google_cookie_status(raw)
    return bool(status["flow_cookies_configured"] and status["google_session_cookies_configured"])
