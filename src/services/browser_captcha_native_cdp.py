"""Native CDP captcha service with one persistent Chromium profile per token."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
import random
import shutil
import subprocess
import time
import tempfile
from types import SimpleNamespace
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional
from urllib.parse import quote, unquote, urlparse

from ..core.config import config
from ..core.flow_cookies import normalize_google_cookies, has_complete_flow_cookies
from ..core.logger import debug_logger
from ..core.generation_errors import NativeSessionError
from ..core.native_session_state import local_session_state, validate_local_session_proxy
from ..core.browser_profile import configure_web_only_profile
from ..core.media_errors import is_media_traffic_error
from .flow_angular import (AngularProtocolError, AngularSubmissionUncertain, parse_rpc_response,
                          rpc_fetch_expression, FLOW_IDENTITY_EXPRESSION, verified_flow_email,
                          flow_credits, flow_projects, MUTATING_RPC_IDS, RPC_IDS, FLOW_RPC_URL)


FLOW_PROJECT_BASE_URL = "https://labs.google/fx/zh/tools/flow"
ANGULAR_PROJECT_BASE_URL = "https://flow.google.com"
FLOW_WEBSITE_KEY = "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV"
DEFAULT_PROFILE_ROOT = Path("tmp") / "native_cdp_profiles"
DEFAULT_IDLE_TTL_SECONDS = 600
DEFAULT_VIDEO_SUBMIT_RESERVATION_SECONDS = 60
DEFAULT_FRESH_PROFILE_RESTART_EVERY_N_SOLVES = 10
PROFILE_STATE_VERSION = "2"
PROFILE_STATE_VERSION_FILE = ".flow2api-native-cdp-version"


def _is_recaptcha_profile_risk_error(error_message: Any) -> bool:
    """Return whether Flow rejected the browser's reCAPTCHA risk state."""
    error_lower = str(error_message or "").lower()
    if "recaptcha evaluation failed" not in error_lower:
        return False
    return any(
        marker in error_lower
        for marker in (
            "public_error_unusual_activity",
            "public_error_something_went_wrong",
            "unusual_activity",
            "unusual activity",
        )
    )


def _mask_proxy(proxy_url: Optional[str]) -> str:
    if not proxy_url:
        return "none"
    try:
        parsed = urlparse(proxy_url)
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        return f"{parsed.scheme or 'http'}://{host}{port}"
    except Exception:
        return "configured"


def _parse_proxy_url(proxy_url: str) -> tuple[str, str, int, Optional[str], Optional[str]]:
    value = str(proxy_url or "").strip()
    if not value:
        raise RuntimeError("native_cdp requires a token or global browser proxy")
    if "://" not in value:
        value = f"http://{value}"
    parsed = urlparse(value)
    scheme = (parsed.scheme or "http").lower()
    if scheme not in {"http", "https", "socks4", "socks5"}:
        raise RuntimeError(f"unsupported native_cdp proxy scheme: {scheme}")
    if not parsed.hostname or not parsed.port:
        raise RuntimeError("invalid native_cdp proxy URL")
    return (
        scheme,
        parsed.hostname,
        int(parsed.port),
        unquote(parsed.username) if parsed.username is not None else None,
        unquote(parsed.password) if parsed.password is not None else None,
    )


def _proxy_egress_key(proxy_url: str) -> str:
    """Return a credential-free stable key for one configured proxy endpoint.

    Native CDP profiles may refer to the Docker host through localhost,
    127.0.0.1, or host.docker.internal.  Those aliases reach the same xray
    listener, so normalize them before grouping accounts.  Credentials are
    deliberately excluded: the endpoint is the observable egress boundary and
    secrets must never enter diagnostics.
    """
    scheme, host, port, username, _ = _parse_proxy_url(proxy_url)
    normalized_host = str(host or "").strip().lower().strip("[]")
    is_local_listener = normalized_host in {
        "localhost",
        "127.0.0.1",
        "0.0.0.0",
        "::1",
        "host.docker.internal",
    }
    if is_local_listener:
        normalized_host = "local-xray"
        endpoint = f"{normalized_host}:{int(port)}"
    else:
        # Remote proxy gateways can route credentials to different exits. Keep
        # those identities separate without retaining or exposing the username.
        auth_identity = hashlib.sha256(
            str(username or "").encode("utf-8")
        ).hexdigest()
        endpoint = f"{scheme}://{normalized_host}:{int(port)}#{auth_identity}"
    return hashlib.sha256(endpoint.encode("utf-8")).hexdigest()


def _timestamp_seconds(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _detect_browser_executable() -> Optional[str]:
    configured = str(os.environ.get("BROWSER_EXECUTABLE_PATH") or "").strip()
    if configured and os.path.isfile(configured):
        return configured

    candidates = []
    if platform.system().lower() == "windows":
        roots = [
            os.environ.get("PROGRAMFILES"),
            os.environ.get("PROGRAMFILES(X86)"),
            os.environ.get("LOCALAPPDATA"),
        ]
        suffixes = [
            ("Google", "Chrome", "Application", "chrome.exe"),
            ("Microsoft", "Edge", "Application", "msedge.exe"),
        ]
        for root in roots:
            if not root:
                continue
            candidates.extend(os.path.join(root, *suffix) for suffix in suffixes)
    else:
        candidates.extend(
            [
                "/usr/bin/google-chrome",
                "/usr/bin/google-chrome-stable",
                "/usr/bin/chromium",
                "/usr/bin/chromium-browser",
                "/snap/bin/chromium",
            ]
        )

    for candidate in candidates:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    for binary in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "msedge"):
        resolved = shutil.which(binary)
        if resolved:
            return resolved
    return None


def _profile_root() -> Path:
    configured = str(os.environ.get("NATIVE_CDP_PROFILE_ROOT") or "").strip()
    root = Path(configured) if configured else DEFAULT_PROFILE_ROOT
    return root.resolve()


def _create_proxy_auth_extension(
    profile_dir: Path,
    scheme: str,
    host: str,
    port: int,
    username: str,
    password: str,
) -> Path:
    extension_dir = profile_dir / ".runtime_proxy_extension"
    if extension_dir.exists():
        shutil.rmtree(extension_dir, ignore_errors=True)
    extension_dir.mkdir(parents=True, exist_ok=True)

    manifest = {
        "manifest_version": 3,
        "name": "Native CDP Proxy",
        "version": "1.0.0",
        "permissions": ["proxy", "storage", "webRequest", "webRequestAuthProvider"],
        "host_permissions": ["<all_urls>"],
        "background": {"service_worker": "background.js"},
    }
    proxy_scheme = "socks5" if scheme == "socks5" else "socks4" if scheme == "socks4" else "http"
    background = f"""
const config = {{
  mode: "fixed_servers",
  rules: {{
    singleProxy: {{scheme: {json.dumps(proxy_scheme)}, host: {json.dumps(host)}, port: {int(port)}}},
    bypassList: ["localhost", "127.0.0.1"]
  }}
}};
chrome.proxy.settings.set({{value: config, scope: "regular"}});
chrome.webRequest.onAuthRequired.addListener(
  () => ({{authCredentials: {{username: {json.dumps(username)}, password: {json.dumps(password)}}}}}),
  {{urls: ["<all_urls>"]}},
  ["blocking"]
);
"""
    (extension_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (extension_dir / "background.js").write_text(background, encoding="utf-8")
    return extension_dir


class CdpProtocolError(RuntimeError):
    pass


EventHandler = Callable[[Dict[str, Any]], Awaitable[None] | None]


class CdpConnection:
    def __init__(self, websocket_url: str):
        self.websocket_url = websocket_url
        self._websocket = None
        self._reader_task: Optional[asyncio.Task] = None
        self._next_id = 0
        self._pending: Dict[int, asyncio.Future] = {}
        self._waiters: list[tuple[str, Optional[str], asyncio.Future]] = []
        self._handlers: Dict[str, list[EventHandler]] = {}
        self._send_lock = asyncio.Lock()
        self.closed = False

    async def connect(self) -> None:
        import websockets

        self._websocket = await websockets.connect(
            self.websocket_url,
            open_timeout=15,
            close_timeout=5,
            max_size=None,
            ping_interval=20,
            ping_timeout=20,
        )
        self.closed = False
        self._reader_task = asyncio.create_task(self._reader_loop())

    async def _reader_loop(self) -> None:
        try:
            async for raw_message in self._websocket:
                message = json.loads(raw_message)
                message_id = message.get("id")
                if message_id is not None:
                    future = self._pending.pop(int(message_id), None)
                    if future and not future.done():
                        if message.get("error"):
                            error = message["error"]
                            future.set_exception(
                                CdpProtocolError(
                                    f"{error.get('code', 'CDP')}: {error.get('message', 'unknown error')}"
                                )
                            )
                        else:
                            future.set_result(message.get("result") or {})
                    continue

                method = str(message.get("method") or "")
                session_id = message.get("sessionId")
                params = message.get("params") or {}
                for waiter in list(self._waiters):
                    waiter_method, waiter_session, future = waiter
                    if method != waiter_method:
                        continue
                    if waiter_session is not None and waiter_session != session_id:
                        continue
                    self._waiters.remove(waiter)
                    if not future.done():
                        future.set_result(params)

                for handler in list(self._handlers.get(method, [])):
                    try:
                        result = handler({"params": params, "sessionId": session_id})
                        if asyncio.iscoroutine(result):
                            asyncio.create_task(result)
                    except Exception as exc:
                        debug_logger.log_warning(
                            f"[NativeCDP] event handler failed ({method}): {type(exc).__name__}: {exc}"
                        )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self.closed:
                debug_logger.log_warning(
                    f"[NativeCDP] websocket reader stopped: {type(exc).__name__}: {exc}"
                )
        finally:
            self.closed = True
            error = ConnectionError("native CDP websocket disconnected")
            for future in list(self._pending.values()):
                if not future.done():
                    future.set_exception(error)
            self._pending.clear()
            for _, _, future in list(self._waiters):
                if not future.done():
                    future.set_exception(error)
            self._waiters.clear()

    async def send(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        session_id: Optional[str] = None,
        timeout: float = 30,
    ) -> Dict[str, Any]:
        if self.closed or self._websocket is None:
            raise ConnectionError("native CDP websocket is not connected")
        async with self._send_lock:
            self._next_id += 1
            message_id = self._next_id
            future = asyncio.get_running_loop().create_future()
            self._pending[message_id] = future
            payload: Dict[str, Any] = {
                "id": message_id,
                "method": method,
                "params": params or {},
            }
            if session_id:
                payload["sessionId"] = session_id
            try:
                await self._websocket.send(json.dumps(payload))
            except Exception:
                self._pending.pop(message_id, None)
                raise
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._pending.pop(message_id, None)

    async def wait_event(
        self,
        method: str,
        *,
        session_id: Optional[str] = None,
        timeout: float = 30,
    ) -> Dict[str, Any]:
        future = asyncio.get_running_loop().create_future()
        waiter = (method, session_id, future)
        self._waiters.append(waiter)
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            if waiter in self._waiters:
                self._waiters.remove(waiter)

    def add_handler(self, method: str, handler: EventHandler) -> None:
        self._handlers.setdefault(method, []).append(handler)

    async def close(self) -> None:
        if self.closed and self._reader_task is None:
            return
        self.closed = True
        if self._websocket is not None:
            try:
                await self._websocket.close()
            except Exception:
                pass
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        self._reader_task = None
        self._websocket = None


@dataclass
class ProxyBinding:
    url: str
    source: str

    @property
    def signature(self) -> str:
        return self.url


@dataclass
class ProxyRiskState:
    """Traffic-control state shared by every account on one proxy egress."""

    failure_streak: int = 0
    cooldown_until: float = 0.0
    last_failure_at: Optional[float] = None
    last_success_at: Optional[float] = None
    last_error: str = ""
    token_ids: set[int] = field(default_factory=set)
    last_failure_token_id: Optional[int] = None
    last_failure_monotonic: float = 0.0


class NativeCdpAccountBrowser:
    def __init__(self, token_id: int, db):
        self.token_id = int(token_id)
        self.db = db
        self.profile_dir = _profile_root() / f"token-{self.token_id}"
        self.process: Optional[subprocess.Popen] = None
        self.connection: Optional[CdpConnection] = None
        self.proxy_binding: Optional[ProxyBinding] = None
        self.proxy_extension_dir: Optional[Path] = None
        self.solve_lock = asyncio.Lock()
        self.busy_count = 0
        self.last_used_at = time.monotonic()
        self.last_started_at: Optional[float] = None
        self.last_error: Optional[str] = None
        self.last_upstream_error: Optional[str] = None
        self.last_fingerprint: Optional[Dict[str, Any]] = None
        self.solve_count = 0
        self.profile_solve_count = 0
        self.profile_reset_count = 0
        self._profile_reset_pending = False
        self._profile_reset_reason = ""
        self._requires_flow_login = False
        self._legacy_migrated = False
        if self.profile_dir.exists():
            try:
                profile_version = (
                    self.profile_dir / PROFILE_STATE_VERSION_FILE
                ).read_text(encoding="utf-8").strip()
            except (FileNotFoundError, OSError, UnicodeError):
                profile_version = ""
            if profile_version != PROFILE_STATE_VERSION:
                self._profile_reset_pending = True
                self._profile_reset_reason = "profile_baseline_upgrade"
        self._project_sessions: Dict[tuple[str, str], tuple[str, str]] = {}
        self._session_auth_signatures: Dict[str, str] = {}
        self._cookie_seed_signature = ""
        self._video_submit_reservations: list[float] = []

    @property
    def is_running(self) -> bool:
        return bool(
            self.process
            and self.process.poll() is None
            and self.connection
            and not self.connection.closed
        )

    @property
    def is_busy(self) -> bool:
        self._prune_video_submit_reservations()
        return (
            self.busy_count > 0
            or self.solve_lock.locked()
            or bool(self._video_submit_reservations)
        )

    def _prune_video_submit_reservations(self) -> None:
        now = time.monotonic()
        self._video_submit_reservations = [
            deadline
            for deadline in self._video_submit_reservations
            if deadline > now
        ]

    def reserve_for_video_submit(
        self,
        ttl_seconds: int = DEFAULT_VIDEO_SUBMIT_RESERVATION_SECONDS,
    ) -> None:
        self._prune_video_submit_reservations()
        self._video_submit_reservations.append(
            time.monotonic() + max(5, int(ttl_seconds))
        )

    def consume_video_submit_reservation(self) -> None:
        self._prune_video_submit_reservations()
        if self._video_submit_reservations:
            self._video_submit_reservations.pop(0)

    def _fresh_profile_restart_threshold(self) -> int:
        value = getattr(
            config,
            "native_cdp_fresh_restart_every_n_solves",
            DEFAULT_FRESH_PROFILE_RESTART_EVERY_N_SOLVES,
        )
        try:
            return max(0, int(value))
        except Exception:
            return DEFAULT_FRESH_PROFILE_RESTART_EVERY_N_SOLVES

    def _mark_profile_reset_pending(self, reason: str) -> None:
        self._profile_reset_pending = True
        self._profile_reset_reason = str(reason or "recaptcha_risk")[:160]

    async def _reset_profile(self, *, reason: str) -> None:
        normalized_reason = str(reason or "scheduled")[:160]
        await self.stop(reason=f"profile_reset:{normalized_reason}")
        shutil.rmtree(self.profile_dir, ignore_errors=True)
        self._cookie_seed_signature = ""
        self._session_auth_signatures.clear()
        self.profile_solve_count = 0
        self.profile_reset_count += 1
        self._profile_reset_pending = False
        self._profile_reset_reason = ""
        self.last_fingerprint = None
        debug_logger.log_warning(
            f"[NativeCDP] reset fresh profile token={self.token_id}, "
            f"reason={normalized_reason}, resets={self.profile_reset_count}"
        )

    async def _prepare_profile(self, *, for_solve: bool) -> None:
        reset_reason = ""
        if self._profile_reset_pending:
            reset_reason = self._profile_reset_reason or "recaptcha_risk"
        elif for_solve:
            threshold = self._fresh_profile_restart_threshold()
            if threshold > 0 and self.profile_solve_count >= threshold:
                reset_reason = f"solve_threshold_{threshold}"
        if reset_reason:
            token = await self.db.get_token(self.token_id)
            if local_session_state(self.token_id, self.profile_dir) or getattr(token, "google_cookies", None):
                # A signed-in Google profile can contain device-bound sessions
                # and locally rotated credentials that cookie export cannot restore.
                # Routine/risk recovery may restart it, but must not erase it.
                await self.stop(reason=f"authenticated_profile_restart:{reset_reason}")
                self.profile_solve_count = 0
                self._profile_reset_pending = False
                self._profile_reset_reason = ""
            else:
                await self._reset_profile(reason=reset_reason)
        await self.start()

    async def _resolve_proxy(self) -> ProxyBinding:
        token = await self.db.get_token(self.token_id)
        token_proxy = str(getattr(token, "captcha_proxy_url", "") or "").strip() if token else ""
        if token_proxy:
            _parse_proxy_url(token_proxy)
            return ProxyBinding(token_proxy, "token")

        captcha_config = await self.db.get_captcha_config()
        global_proxy = str(getattr(captcha_config, "browser_proxy_url", "") or "").strip()
        if bool(getattr(captcha_config, "browser_proxy_enabled", False)) and global_proxy:
            _parse_proxy_url(global_proxy)
            return ProxyBinding(global_proxy, "global")
        raise RuntimeError(
            f"native_cdp token {self.token_id} has no token proxy and no enabled global browser proxy"
        )

    async def _wait_for_devtools_endpoint(self, timeout: float = 25) -> str:
        active_port_path = self.profile_dir / "DevToolsActivePort"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process and self.process.poll() is not None:
                raise RuntimeError(
                    f"Chromium exited during startup with code {self.process.returncode}"
                )
            try:
                lines = active_port_path.read_text(encoding="utf-8").splitlines()
                if len(lines) >= 2 and lines[0].strip().isdigit():
                    return f"ws://127.0.0.1:{lines[0].strip()}{lines[1].strip()}"
            except (FileNotFoundError, OSError, UnicodeError):
                pass
            await asyncio.sleep(0.1)
        raise TimeoutError("timed out waiting for Chromium DevToolsActivePort")

    async def start(self) -> None:
        proxy_binding = await self._resolve_proxy()
        validate_local_session_proxy(local_session_state(self.token_id, self.profile_dir), proxy_binding.url)
        if self.is_running and self.proxy_binding and self.proxy_binding.signature == proxy_binding.signature:
            return
        proxy_changed = bool(
            self.proxy_binding
            and self.proxy_binding.signature != proxy_binding.signature
        )
        if proxy_changed:
            # A browser identity must not survive an account proxy change.
            await self._reset_profile(reason="proxy_changed")
        elif self.process or self.connection:
            await self.stop(reason="proxy_changed_or_reconnect")

        executable = _detect_browser_executable()
        if not executable:
            raise RuntimeError(
                "native_cdp browser executable not found; configure BROWSER_EXECUTABLE_PATH "
                "or use the headed image"
            )

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        try:
            configure_web_only_profile(self.profile_dir)
        except (OSError, ValueError, TypeError, AttributeError):
            raise NativeSessionError("native_profile_preferences_invalid", stage="browser_startup") from None
        try:
            (self.profile_dir / PROFILE_STATE_VERSION_FILE).write_text(
                PROFILE_STATE_VERSION,
                encoding="utf-8",
            )
        except OSError as exc:
            debug_logger.log_warning(
                f"[NativeCDP] failed to persist profile baseline token={self.token_id}: {exc}"
            )
        active_port_path = self.profile_dir / "DevToolsActivePort"
        try:
            active_port_path.unlink(missing_ok=True)
        except OSError:
            pass

        scheme, host, port, username, password = _parse_proxy_url(proxy_binding.url)
        args = [
            executable,
            f"--user-data-dir={self.profile_dir}",
            "--remote-debugging-address=127.0.0.1",
            "--remote-debugging-port=0",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-component-update",
            "--disable-features=Translate,OptimizationHints",
            "--disable-blink-features=AutomationControlled",
            "--window-size=1440,900",
            "about:blank",
        ]
        if os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() == 0:
            args.append("--no-sandbox")
        if username is not None and password is not None:
            self.proxy_extension_dir = _create_proxy_auth_extension(
                self.profile_dir,
                scheme,
                host,
                port,
                username,
                password,
            )
            args.extend(
                [
                    f"--disable-extensions-except={self.proxy_extension_dir}",
                    f"--load-extension={self.proxy_extension_dir}",
                ]
            )
        else:
            args.append(f"--proxy-server={scheme}://{host}:{port}")

        debug_logger.log_info(
            f"[NativeCDP] starting token={self.token_id}, profile={self.profile_dir.name}, "
            f"proxy_source={proxy_binding.source}, proxy={_mask_proxy(proxy_binding.url)}"
        )
        creation_flags = 0
        if os.name == "nt":
            creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.process = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation_flags,
        )
        try:
            websocket_url = await self._wait_for_devtools_endpoint()
            connection = CdpConnection(websocket_url)
            await connection.connect()
            await connection.send("Target.setDiscoverTargets", {"discover": True})
            self.connection = connection
            self.proxy_binding = proxy_binding
            self.last_started_at = time.monotonic()
            self.last_error = None
        except Exception:
            await self.stop(reason="startup_failed")
            raise

    async def stop(self, *, reason: str) -> None:
        connection = self.connection
        process = self.process
        self.connection = None
        self.process = None
        self._project_sessions.clear()
        self._session_auth_signatures.clear()
        self._video_submit_reservations.clear()
        if connection:
            try:
                await connection.send("Browser.close", timeout=3)
            except Exception:
                pass
            await connection.close()
        if process and process.poll() is None:
            try:
                # Browser.close starts asynchronous shutdown. Give Chromium time
                # to flush Cookies/session state before escalating to OS signals.
                # Use Popen's bounded wait so timeout leaves no waiting thread.
                await asyncio.to_thread(process.wait, timeout=8)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    await asyncio.to_thread(process.wait, timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    await asyncio.to_thread(process.wait, timeout=3)
                debug_logger.log_runtime_event(
                    "native_browser_forced_shutdown", token_id=self.token_id,
                    stage="browser_shutdown", reason="graceful_shutdown_timeout",
                )
        if self.proxy_extension_dir and self.proxy_extension_dir.exists():
            shutil.rmtree(self.proxy_extension_dir, ignore_errors=True)
        self.proxy_extension_dir = None
        debug_logger.log_info(
            f"[NativeCDP] stopped token={self.token_id}, reason={reason}, profile_preserved=true"
        )

    async def delete_profile(self) -> None:
        await self.stop(reason="token_deleted")
        shutil.rmtree(self.profile_dir, ignore_errors=True)

    async def _create_page_session(self) -> tuple[str, str]:
        if not self.connection:
            raise ConnectionError("native CDP browser is not connected")
        target_result = await self.connection.send(
            "Target.createTarget",
            {"url": "about:blank", "newWindow": False, "background": False},
        )
        target_id = str(target_result.get("targetId") or "")
        if not target_id:
            raise CdpProtocolError("Target.createTarget returned no targetId")
        attach_result = await self.connection.send(
            "Target.attachToTarget",
            {"targetId": target_id, "flatten": True},
        )
        session_id = str(attach_result.get("sessionId") or "")
        if not session_id:
            raise CdpProtocolError("Target.attachToTarget returned no sessionId")
        await self.connection.send("Page.enable", session_id=session_id)
        await self.connection.send("Runtime.enable", session_id=session_id)
        await self.connection.send("Network.enable", session_id=session_id)
        await self.connection.send(
            "Page.addScriptToEvaluateOnNewDocument",
            {
                "source": """
                    (() => {
                      try {
                        Object.defineProperty(Navigator.prototype, 'webdriver', {
                          get: () => undefined,
                          configurable: true
                        });
                      } catch (_) {}
                    })();
                """,
            },
            session_id=session_id,
        )
        return target_id, session_id

    async def _evaluate(
        self,
        session_id: str,
        expression: str,
        *,
        await_promise: bool = False,
        timeout: float = 30,
    ) -> Any:
        if not self.connection:
            raise ConnectionError("native CDP browser is not connected")
        result = await self.connection.send(
            "Runtime.evaluate",
            {
                "expression": expression,
                "awaitPromise": await_promise,
                "returnByValue": True,
                "userGesture": True,
            },
            session_id=session_id,
            timeout=timeout,
        )
        if result.get("exceptionDetails"):
            details = result["exceptionDetails"]
            description = (
                (details.get("exception") or {}).get("description")
                or details.get("text")
                or "JavaScript evaluation failed"
            )
            raise CdpProtocolError(str(description))
        return (result.get("result") or {}).get("value")

    async def _seed_session_cookie(self, session_id: str, page_protocol: str = "labs") -> bool:
        if not self.connection:
            return False
        if local_session_state(self.token_id, self.profile_dir):
            # This profile owns its login. Neither Labs ST nor Google cookies
            # from the external updater may replace its locally rotated session.
            return False
        token = await self.db.get_token(self.token_id)
        session_token = (str(getattr(token, "st", "") or "").strip()
                         if token and getattr(token, "auth_mode", "labs") != "flow" else "")
        raw_cookies = (str(getattr(token, "google_cookies", "") or "")
                       if token and page_protocol == "angular" else "")
        if raw_cookies and not has_complete_flow_cookies(raw_cookies):
            raise NativeSessionError("google_session_cookies_incomplete", protocol=page_protocol)
        self._requires_flow_login = bool(raw_cookies)
        signature = hashlib.sha256((session_token + "\0" + raw_cookies).encode()).hexdigest()
        marker = self.profile_dir / ".flow2api-cookie-seed"
        if not self._cookie_seed_signature:
            try:
                self._cookie_seed_signature = marker.read_text(encoding="utf-8").strip()
            except OSError:
                pass
        # A seed marker outlives session-only NextAuth cookies. Check the live
        # jar as well, otherwise an idle restart silently loses Labs login.
        current = (await self.connection.send("Network.getCookies", {"urls": ["https://labs.google/"]},
                                             session_id=session_id, timeout=5)
                   if session_token else {})
        labs_session_present = any(
            cookie.get("name", "").startswith("__Secure-next-auth.session-token")
            and cookie.get("value")
            for cookie in current.get("cookies", [])
        )
        reseed_missing_st = bool(session_token and not labs_session_present)
        if signature != self._cookie_seed_signature or reseed_missing_st:
            google_signature = hashlib.sha256(raw_cookies.encode()).hexdigest()
            google_marker = self.profile_dir / ".flow2api-google-cookie-seed"
            try:
                imported_google_signature = google_marker.read_text(encoding="utf-8").strip()
            except OSError:
                imported_google_signature = ""
            cookies = (json.loads(normalize_google_cookies(raw_cookies))
                       if raw_cookies and google_signature != imported_google_signature else [])
            if session_token:
                # Remove stale chunked NextAuth cookies before replacing the session.
                for cookie in current.get("cookies", []):
                    if cookie.get("name", "").startswith("__Secure-next-auth.session-token"):
                        await self.connection.send("Network.deleteCookies", {key: cookie[key] for key in ("name", "domain", "path")}, session_id=session_id, timeout=5)
                chunks = [session_token[i:i + 3800] for i in range(0, len(session_token), 3800)]
                cookies.extend({"name": "__Secure-next-auth.session-token" + (f".{i}" if len(chunks) > 1 else ""),
                                "value": value, "domain": "labs.google", "path": "/", "secure": True,
                                "httpOnly": True, "sameSite": "Lax"} for i, value in enumerate(chunks))
            for cookie in cookies:
                params = dict(cookie)
                if not params["domain"].startswith("."):
                    params["url"] = "https://" + params.pop("domain") + params["path"]
                result = await self.connection.send("Network.setCookie", params, session_id=session_id, timeout=5)
                if result.get("success") is False:
                    raise RuntimeError("Browser rejected a synchronized authentication cookie")
            self._cookie_seed_signature = signature
            # Hash only. Reopening the persistent browser must not restore an old
            # snapshot over Google cookies that it has already rotated locally.
            self.profile_dir.mkdir(parents=True, exist_ok=True)
            marker.write_text(signature, encoding="utf-8")
            if page_protocol == "angular":
                google_marker.write_text(google_signature, encoding="utf-8")
        changed = reseed_missing_st or self._session_auth_signatures.get(session_id) != signature
        self._session_auth_signatures[session_id] = signature
        return changed

    @staticmethod
    def _project_page_url(project_id: Optional[str], page_protocol: str = "labs") -> str:
        if page_protocol not in {"labs", "angular"}:
            raise ValueError("Unknown native page protocol")
        base_url = ANGULAR_PROJECT_BASE_URL if page_protocol == "angular" else FLOW_PROJECT_BASE_URL
        normalized_project_id = str(project_id or "").strip()
        if not normalized_project_id:
            return base_url
        return f"{base_url}/project/{quote(normalized_project_id, safe='')}"

    async def _wait_for_document_ready(self, session_id: str, timeout: float = 35) -> None:
        deadline = time.monotonic() + timeout
        complete_observations = 0
        while time.monotonic() < deadline:
            try:
                ready_state = await self._evaluate(
                    session_id,
                    "document.readyState",
                    timeout=3,
                )
                if ready_state == "complete":
                    complete_observations += 1
                    if complete_observations >= 3:
                        return
                else:
                    complete_observations = 0
            except Exception:
                complete_observations = 0
            await asyncio.sleep(0.25)
        raise TimeoutError("real Flow project page did not become ready")

    async def _open_real_project_page(
        self,
        session_id: str,
        project_id: str,
        page_protocol: str = "labs",
    ) -> None:
        if not self.connection:
            raise ConnectionError("native CDP browser is not connected")
        page_url = self._project_page_url(project_id, page_protocol)
        navigation = await self.connection.send(
            "Page.navigate",
            {"url": page_url},
            session_id=session_id,
            timeout=45,
        )
        if navigation.get("errorText"):
            raise NativeSessionError("page_navigation_failed", protocol=page_protocol, page_url=page_url)
        try:
            await self._wait_for_document_ready(session_id)
        except TimeoutError as exc:
            # A stalled nonessential resource can keep load pending even after
            # Angular is authenticated and usable. Accept only the exact project
            # URL plus its live bootstrap, never a login/about/other project page.
            if page_protocol == "angular":
                try:
                    await self._validate_project_page(session_id, project_id, page_protocol)
                    return
                except NativeSessionError as validation:
                    if validation.page_path.rstrip("/") == "/about":
                        raise validation from exc
            try:
                actual_url = str(await self._evaluate(session_id, "window.location.href", timeout=3) or page_url)
            except Exception:
                actual_url = page_url
            raise NativeSessionError("page_not_ready", protocol=page_protocol, page_url=actual_url) from exc
        await self._validate_project_page(session_id, project_id, page_protocol, wait_seconds=8)

    async def _validate_project_page(self, session_id, project_id, page_protocol, *, wait_seconds=0):
        expected = urlparse(self._project_page_url(project_id, page_protocol))
        deadline = time.monotonic() + wait_seconds
        while True:
            current_url = str(await self._evaluate(session_id, "window.location.href", timeout=5) or "")
            parsed = urlparse(current_url)
            matches = (parsed.scheme == "https" and parsed.hostname == expected.hostname
                       and parsed.path.rstrip("/") == expected.path.rstrip("/"))
            reason = "project_context_unavailable"
            if matches:
                if page_protocol == "labs":
                    return
                # Always verify Angular bootstrap, even without synchronized cookies.
                if await self._evaluate(session_id, "!!window.WIZ_global_data?.SNlM0e", timeout=5):
                    return
                reason = "flow_login_unavailable"
            if time.monotonic() >= deadline:
                raise NativeSessionError(reason, protocol=page_protocol, page_url=current_url)
            await asyncio.sleep(0.25)

    async def _get_or_create_project_session(
        self,
        project_id: str,
        page_protocol: str = "labs",
    ) -> tuple[str, str]:
        if page_protocol == "labs" and self.db is not None:
            token = await self.db.get_token(self.token_id)
            revision = hashlib.sha256((str(getattr(token, "st", "") or "") + "\0"
                                       + str(getattr(token, "google_cookies", "") or "")).encode()).hexdigest()
            if getattr(self, "_legacy_auth_revision", None) != revision:
                # A redirect observed with old credentials must not permanently
                # force new Labs sessions onto an unauthenticated Angular page.
                self._legacy_migrated = False
                self._legacy_auth_revision = revision
            if (local_session_state(self.token_id, self.profile_dir)
                    or has_complete_flow_cookies(getattr(token, "google_cookies", None))):
                # A complete modern session selects its authenticated page,
                # independently from the REST/RPC generation wire format.
                self._legacy_migrated = True
        if page_protocol == "labs" and self._legacy_migrated:
            return await self._get_or_create_project_session(project_id, "angular")
        normalized_project_id = str(project_id or "").strip()
        cache_key = (page_protocol, normalized_project_id)
        cached = self._project_sessions.get(cache_key)
        if cached:
            target_id, session_id = cached
            try:
                await self._evaluate(session_id, "document.readyState", timeout=3)
                if await self._seed_session_cookie(session_id, page_protocol):
                    await self._open_real_project_page(session_id, normalized_project_id, page_protocol)
                else:
                    await self._validate_project_page(session_id, normalized_project_id, page_protocol)
                return target_id, session_id
            except Exception:
                await self._discard_project_session(normalized_project_id, page_protocol)

        target_id, session_id = await self._create_page_session()
        try:
            await self._seed_session_cookie(session_id, page_protocol)
            await self._open_real_project_page(session_id, normalized_project_id, page_protocol)
            await self._capture_fingerprint(session_id)
        except Exception as exc:
            if self.connection and not self.connection.closed:
                try:
                    await self.connection.send(
                        "Target.closeTarget",
                        {"targetId": target_id},
                        timeout=5,
                    )
                except Exception:
                    pass
            if (page_protocol == "labs" and isinstance(exc, NativeSessionError)
                    and exc.page_origin == ANGULAR_PROJECT_BASE_URL):
                # The old site now performs a client-side migration. This is a
                # preflight-only page transition, never a generation resubmit.
                self._legacy_migrated = True
                debug_logger.log_runtime_event("native_legacy_page_migrated", token_id=self.token_id,
                                               stage="browser_preflight", reason="legacy_site_redirect", protocol="angular")
                return await self._get_or_create_project_session(project_id, "angular")
            raise
        self._project_sessions[cache_key] = (target_id, session_id)
        return target_id, session_id

    async def verify_session(self, project_id: str) -> None:
        """Non-charging Flow page preflight, serialized with this account's work."""
        async with self.solve_lock:
            try:
                await self._prepare_profile(for_solve=False)
                await self._get_or_create_project_session(project_id, "angular")
                self.last_error = None
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {str(exc)[:240]}"
                if isinstance(exc, NativeSessionError):
                    debug_logger.log_runtime_event("native_sync_preflight_failed", token_id=self.token_id, **exc.diagnostic())
                raise

    async def flow_account_snapshot(self, expected_email: str = "") -> Dict[str, Any]:
        """Read identity, credits and existing projects in one authenticated Flow page."""
        async with self.solve_lock:
            try:
                result = await self._flow_account_snapshot_locked(expected_email)
                self.last_error = None
                return result
            except Exception as exc:
                # Keep diagnostics even when periodic credit probing runs with
                # debug logging disabled. Never store page contents or credentials.
                self.last_error = exc.reason if isinstance(exc, NativeSessionError) else type(exc).__name__
                debug_logger.log_runtime_event("native_account_probe_failed", token_id=self.token_id,
                    **(exc.diagnostic() if isinstance(exc, NativeSessionError) else
                       {"stage": "account_verification", "reason": "verification_unavailable"}))
                raise

    async def _flow_account_snapshot_locked(self, expected_email: str) -> Dict[str, Any]:
        await self._prepare_profile(for_solve=False)
        _, session_id = await self._get_or_create_project_session("", "angular")
        try:
            identity = await self._evaluate(session_id, FLOW_IDENTITY_EXPRESSION)
            email = verified_flow_email(identity, expected_email)
        except AngularProtocolError as exc:
            reason = "flow_identity_mismatch" if "mismatch" in str(exc) else "flow_identity_unavailable"
            raise NativeSessionError(reason, protocol="angular", stage="account_identity") from None
        async def rpc(rpc_id, payload):
            result = await self._evaluate(session_id, rpc_fetch_expression(rpc_id, payload, 20),
                                          await_promise=True, timeout=25)
            if not isinstance(result, dict) or result.get("status") != 200:
                raise NativeSessionError("flow_account_probe_failed", protocol="angular", stage="account_verification")
            return parse_rpc_response(result.get("text", ""), rpc_id)
        credits = flow_credits(await rpc("nzlxg", []))
        projects = flow_projects(await rpc("UpteDb", ["projects/*", 21, None, None, None, None, [1]]))
        # Recheck identity after the requests (account switch/navigation races).
        verified_flow_email(await self._evaluate(session_id, FLOW_IDENTITY_EXPRESSION), email)
        jar = await self.connection.send("Network.getCookies", {"urls": [
            "https://google.com/", "http://google.com/", "https://flow.google.com/",
            "https://accounts.google.com/", "https://www.google.com/"]}, session_id=session_id, timeout=5)
        cookies = normalize_google_cookies(jar.get("cookies", []))
        if not has_complete_flow_cookies(cookies):
            raise NativeSessionError("google_session_cookies_incomplete", protocol="angular")
        return {"email": email, **credits, "projects": projects, "google_cookies": cookies}

    async def _discard_project_session(self, project_id: Optional[str], page_protocol: str = "labs") -> None:
        if page_protocol == "labs" and self._legacy_migrated:
            page_protocol = "angular"
        normalized_project_id = str(project_id or "").strip()
        cached = self._project_sessions.pop((page_protocol, normalized_project_id), None)
        if not cached or not self.connection or self.connection.closed:
            return
        target_id, session_id = cached
        self._session_auth_signatures.pop(session_id, None)
        try:
            await self.connection.send(
                "Target.closeTarget",
                {"targetId": target_id},
                timeout=5,
            )
        except Exception:
            pass

    async def _wait_for_recaptcha(self, session_id: str, timeout: float = 35) -> None:
        deadline = time.monotonic() + timeout
        expression = (
            "typeof grecaptcha !== 'undefined' && "
            "typeof grecaptcha.enterprise !== 'undefined' && "
            "typeof grecaptcha.enterprise.execute === 'function'"
        )
        while time.monotonic() < deadline:
            try:
                if await self._evaluate(session_id, expression, timeout=3):
                    return
            except Exception:
                pass
            await asyncio.sleep(0.5)
        raise TimeoutError("grecaptcha.enterprise did not become ready")

    async def _capture_fingerprint(self, session_id: str) -> Dict[str, Any]:
        value = await self._evaluate(
            session_id,
            """JSON.stringify({
              user_agent: navigator.userAgent,
              platform: navigator.platform,
              language: navigator.language,
              languages: Array.from(navigator.languages || []),
              timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
              hardware_concurrency: navigator.hardwareConcurrency,
              device_memory: navigator.deviceMemory || null,
              webdriver: navigator.webdriver
            })""",
            timeout=5,
        )
        try:
            fingerprint = json.loads(value) if isinstance(value, str) else {}
        except Exception:
            fingerprint = {}
        if self.proxy_binding:
            fingerprint["proxy_url"] = self.proxy_binding.url
            fingerprint["proxy_source"] = self.proxy_binding.source
        self.last_fingerprint = fingerprint
        return fingerprint

    @staticmethod
    def _browser_fetch_headers(headers: Optional[Dict[str, Any]]) -> Dict[str, str]:
        forbidden_names = {
            "accept-encoding",
            "connection",
            "content-length",
            "cookie",
            "host",
            "origin",
            "referer",
            "user-agent",
        }
        filtered: Dict[str, str] = {}
        for key, value in (headers or {}).items():
            if value is None:
                continue
            key_text = str(key or "").strip()
            key_lower = key_text.lower()
            if not key_text or key_lower in forbidden_names:
                continue
            if key_lower.startswith("sec-") or key_lower.startswith("proxy-"):
                continue
            filtered[key_text] = str(value)
        return filtered

    @staticmethod
    def _format_browser_fetch_http_error(status: int, text: str) -> str:
        reason = f"HTTP Error {status}"
        try:
            payload = json.loads(text or "{}")
            error_info = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(error_info, dict):
                message = str(error_info.get("message") or "").strip()
                for detail in error_info.get("details") or []:
                    if isinstance(detail, dict) and detail.get("reason"):
                        reason = str(detail["reason"])
                        break
                if message:
                    reason = f"{reason}: {message}"
        except Exception:
            body = str(text or "").strip()
            if body:
                reason = f"{reason}: {body[:300]}"
        return reason

    async def _upload_flow_video(self, session_id, url, data, timeout):
        args = json.dumps({"url": url, "base64": data["video_base64"], "filename": data.get("filename", "upload.mp4"),
                           "mime": data.get("mime", "video/mp4"), "timeout": timeout * 1000}, ensure_ascii=True)
        expression = """(async () => {
          const args = ARGS;
          const controller = new AbortController();
          const timer = setTimeout(() => controller.abort(), args.timeout);
          try {
            if (location.origin !== 'https://flow.google.com') return {error: 'Flow upload requires an authenticated project page'};
            const bytes = Uint8Array.from(atob(args.base64), c => c.charCodeAt(0));
            const start = await fetch(args.url, {method:'POST', credentials:'include', redirect:'error', signal:controller.signal,
              headers:{'slug':encodeURIComponent(args.filename), 'x-goog-upload-command':'start',
                'x-goog-upload-header-content-length':String(bytes.length), 'x-goog-upload-header-content-type':args.mime,
                'x-goog-upload-protocol':'resumable'}});
            if (!start.ok) return {error:'Flow upload start rejected', status:start.status};
            const sessionLocation = start.headers.get('x-goog-upload-url');
            if (!sessionLocation) return {error:'Flow upload session is missing'};
            const session = new URL(sessionLocation, args.url);
            if (session.origin !== 'https://flow.google.com' || !session.pathname.startsWith('/upload/v1/flow/upload/video/'))
              return {error:'Flow upload session has an unexpected origin'};
            const granularity = Number(start.headers.get('x-goog-upload-chunk-granularity')) || 1048576;
            if (!Number.isSafeInteger(granularity) || granularity < 1) return {error:'Invalid Flow upload granularity'};
            const chunkSize = Math.ceil(1048576 / granularity) * granularity;
            let final = null;
            for (let offset = 0; offset < bytes.length; offset += chunkSize) {
              const chunk = bytes.subarray(offset, Math.min(offset + chunkSize, bytes.length));
              const last = offset + chunk.length === bytes.length;
              const response = await fetch(session.href, {method:'POST', credentials:'include', redirect:'error', signal:controller.signal,
                headers:{'x-goog-upload-command':last ? 'upload, finalize' : 'upload', 'x-goog-upload-offset':String(offset)}, body:chunk});
              if (!response.ok) return {error:'Flow video upload rejected', status:response.status};
              if (last) final = await response.json();
            }
            return {payload:final};
          } catch (_) { return {error:'Flow video upload interrupted'}; }
          finally { clearTimeout(timer); }
        })()""".replace("ARGS", args)
        result = await self._evaluate(session_id, expression, await_promise=True, timeout=timeout + 5)
        if not isinstance(result, dict):
            raise RuntimeError("Invalid Flow upload response")
        if result.get("error"):
            raise RuntimeError(f"{result['error']} (HTTP {result.get('status', 0)})")
        payload = result.get("payload") or {}
        if not isinstance(payload, dict) or not payload.get("mediaId"):
            raise RuntimeError("Flow upload final response is missing mediaId")
        return {**payload, "mediaServerId": payload["mediaId"], "workflowServerId": (payload.get("workflow") or {}).get("name"), "transport": "angular"}

    async def fetch_json(
        self,
        *,
        project_id: str,
        url: str,
        method: str = "POST",
        headers: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None,
        timeout: int = 60,
        page_protocol: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Execute an API request in the token's persistent real Flow project page."""
        async with self.solve_lock:
            self.busy_count += 1
            rpc_id = (json_data or {}).get("rpc_id") if url == FLOW_RPC_URL else None
            rpc_status = None
            rpc_failure_reason = "rpc_preflight_failed"
            rpc_started = time.monotonic()
            try:
                await self._prepare_profile(for_solve=False)
                angular_request = urlparse(url).hostname == "flow.google.com"
                page_protocol = "angular" if angular_request else (page_protocol or "labs")
                _, session_id = await self._get_or_create_project_session(project_id, page_protocol)
                if page_protocol == "labs" and self._legacy_migrated:
                    page_protocol = "angular"
                if url == "https://flow.google.com/_/AiSandboxAngularFrontend/data/batchexecute":
                    rpc_id = (json_data or {}).get("rpc_id")
                    is_submit = rpc_id in MUTATING_RPC_IDS
                    try:
                        rpc_failure_reason = "rpc_evaluation_interrupted"
                        rpc_result = await self._evaluate(session_id, rpc_fetch_expression(rpc_id, json_data["payload"], timeout), await_promise=True, timeout=timeout + 5)
                        rpc_failure_reason = "rpc_result_invalid"
                        if not isinstance(rpc_result, dict):
                            raise AngularProtocolError("Invalid browser RPC result")
                        rpc_status = int(rpc_result.get("status", 0))
                        if rpc_result.get("preflightError"):
                            rpc_failure_reason = "rpc_bootstrap_unavailable"
                            raise AngularProtocolError(rpc_result["preflightError"])
                        if rpc_result.get("fetchError"):
                            rpc_failure_reason = "rpc_fetch_interrupted"
                            raise AngularSubmissionUncertain("Flow launch result is unconfirmed; automatic resubmission is disabled")
                        if rpc_status == 401:
                            rpc_failure_reason = "upstream_authentication_rejected"
                            raise NativeSessionError("upstream_authentication_rejected", protocol="angular", stage="upstream_authentication")
                        if rpc_status >= 400:
                            rpc_failure_reason = "rpc_http_rejected"
                            # Use the shared HTTP error spelling so 429 is routed
                            # to account-egress cooldown, not generic token failure.
                            raise AngularProtocolError(f"Flow RPC rejected: HTTP Error {rpc_status}")
                        try:
                            rpc_failure_reason = "rpc_response_unrecognized"
                            parsed = parse_rpc_response(rpc_result.get("text", ""), rpc_id)
                            self.last_error = None
                            self.last_upstream_error = None
                            return {"rpc_payload": parsed}
                        except AngularProtocolError:
                            if is_submit:
                                raise AngularSubmissionUncertain("Flow launch response is unrecognized; automatic resubmission is disabled") from None
                            raise
                    except (asyncio.TimeoutError, CdpProtocolError, ConnectionError) as exc:
                        rpc_failure_reason = "rpc_cdp_timeout" if isinstance(exc, asyncio.TimeoutError) else "rpc_cdp_interrupted"
                        if is_submit:
                            raise AngularSubmissionUncertain("Flow launch result is unconfirmed; automatic resubmission is disabled") from None
                        raise
                if url == f"https://flow.google.com/upload/v1/flow/upload/video/{quote(str(project_id), safe='')}":
                    uploaded = await self._upload_flow_video(session_id, url, json_data or {}, timeout)
                    self.last_error = None
                    self.last_upstream_error = None
                    return uploaded
                payload = {
                    "url": str(url),
                    "method": str(method or "POST").upper(),
                    "headers": self._browser_fetch_headers(headers),
                    "body": json.dumps(json_data or {}, ensure_ascii=False),
                    "timeoutMs": max(1000, int(timeout * 1000)),
                }
                result = await self._evaluate(
                    session_id,
                    f"""
                    (async () => {{
                      const payload = {json.dumps(payload, ensure_ascii=False)};
                      const controller = new AbortController();
                      const timer = setTimeout(() => controller.abort(), payload.timeoutMs);
                      try {{
                        const response = await fetch(payload.url, {{
                          method: payload.method,
                          headers: payload.headers,
                          body: payload.method === 'GET' ? undefined : payload.body,
                          credentials: 'include',
                          mode: 'cors',
                          signal: controller.signal
                        }});
                        const text = await response.text();
                        return {{
                          status: response.status,
                          statusText: response.statusText || '',
                          text
                        }};
                      }} catch (error) {{
                        return {{
                          fetchError: `${{error && error.name ? error.name : 'Error'}}: ${{error && error.message ? error.message : String(error)}}`
                        }};
                      }} finally {{
                        clearTimeout(timer);
                      }}
                    }})()
                    """,
                    await_promise=True,
                    timeout=max(1, timeout + 5),
                )
                if not isinstance(result, dict):
                    raise RuntimeError("native browser fetch returned invalid result")
                fetch_error = result.get("fetchError")
                if fetch_error:
                    raise RuntimeError(f"native browser fetch failed: {fetch_error}")

                status = int(result.get("status") or 0)
                text = str(result.get("text") or "")
                if status >= 400:
                    upstream_error = self._format_browser_fetch_http_error(status, text)
                    self.last_upstream_error = upstream_error[:240]
                    if status == 401:
                        raise NativeSessionError("upstream_authentication_rejected", protocol=page_protocol,
                                                 stage="upstream_authentication")
                    raise RuntimeError(upstream_error)
                if not text:
                    return {}
                try:
                    parsed = json.loads(text)
                except Exception as exc:
                    raise RuntimeError(
                        f"native browser fetch returned non-JSON response: {text[:300]}"
                    ) from exc
                if not isinstance(parsed, dict):
                    raise RuntimeError(
                        f"native browser fetch returned unexpected JSON type: {type(parsed).__name__}"
                    )
                self.last_error = None
                self.last_upstream_error = None
                return parsed
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {str(exc)[:240]}"
                if url == FLOW_RPC_URL:
                    debug_logger.log_runtime_event("native_rpc_failed", token_id=self.token_id,
                        stage="rpc_request", protocol="angular", rpc_id=rpc_id if rpc_id in RPC_IDS else "unsupported",
                        reason=rpc_failure_reason, status_code=rpc_status,
                        duration_ms=int((time.monotonic() - rpc_started) * 1000))
                if isinstance(exc, NativeSessionError):
                    debug_logger.log_runtime_event("native_preflight_failed", token_id=self.token_id, **exc.diagnostic())
                if _is_recaptcha_profile_risk_error(exc):
                    self._mark_profile_reset_pending("recaptcha_risk_rejected")
                    await self._discard_project_session(project_id, page_protocol or "labs")
                elif "native browser fetch failed" in str(exc).lower():
                    await self._discard_project_session(project_id, page_protocol or "labs")
                raise
            finally:
                self.busy_count = max(0, self.busy_count - 1)
                self.last_used_at = time.monotonic()

    async def solve(
        self,
        project_id: str,
        action: str,
        *,
        website_key: str = FLOW_WEBSITE_KEY,
        page_protocol: str = "labs",
    ) -> Optional[str]:
        async with self.solve_lock:
            self.busy_count += 1
            try:
                await self._prepare_profile(for_solve=True)
                _, session_id = await self._get_or_create_project_session(project_id, page_protocol)
                if page_protocol == "labs" and self._legacy_migrated:
                    page_protocol = "angular"
                try:
                    await self._wait_for_recaptcha(session_id)
                except TimeoutError as exc:
                    raise NativeSessionError("captcha_script_not_ready", protocol=page_protocol) from exc
                await asyncio.sleep(0.8 + random.random())
                await self._evaluate(
                    session_id,
                    """(() => {
                      window.focus();
                      window.dispatchEvent(new Event('focus'));
                      document.dispatchEvent(new MouseEvent('mousemove', {
                        bubbles: true,
                        clientX: 180 + Math.floor(Math.random() * 120),
                        clientY: 120 + Math.floor(Math.random() * 90)
                      }));
                      window.scrollTo(0, 1);
                      return true;
                    })()""",
                    timeout=5,
                )
                await self._capture_fingerprint(session_id)
                token = await self._evaluate(
                    session_id,
                    f"""new Promise((resolve, reject) => {{
                      const timer = setTimeout(() => reject(new Error('captcha timeout')), 30000);
                      grecaptcha.enterprise.ready(() => {{
                        grecaptcha.enterprise.execute({json.dumps(website_key)}, {{
                          action: {json.dumps(action)}
                        }}).then(value => {{
                          clearTimeout(timer);
                          resolve(value);
                        }}).catch(error => {{
                          clearTimeout(timer);
                          reject(error);
                        }});
                      }});
                    }})""",
                    await_promise=True,
                    timeout=35,
                )
                if not isinstance(token, str) or not token.strip():
                    raise RuntimeError("native_cdp returned an empty captcha token")
                settle_seconds = float(getattr(config, "browser_recaptcha_settle_seconds", 3) or 3)
                if settle_seconds > 0:
                    await asyncio.sleep(min(10.0, settle_seconds))
                self.solve_count += 1
                self.profile_solve_count += 1
                self.last_error = None
                debug_logger.log_info(
                    f"[NativeCDP] captcha acquired token_id={self.token_id}, "
                    f"project_id={project_id}, action={action}, solves={self.solve_count}"
                )
                return token.strip()
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {str(exc)[:240]}"
                debug_logger.log_warning(
                    f"[NativeCDP] solve failed token_id={self.token_id}, "
                    f"project_id={project_id}: {self.last_error}"
                )
                await self._discard_project_session(project_id, page_protocol)
                if not self.is_running:
                    await self.stop(reason="runtime_disconnected")
                failure = exc if isinstance(exc, NativeSessionError) else NativeSessionError(
                    "captcha_execution_failed", protocol=page_protocol)
                self.last_error = str(failure)
                debug_logger.log_runtime_event("native_preflight_failed", token_id=self.token_id, **failure.diagnostic())
                if failure is exc:
                    raise
                raise failure from exc
            finally:
                self.busy_count = max(0, self.busy_count - 1)
                self.last_used_at = time.monotonic()

    def status(self) -> Dict[str, Any]:
        self._prune_video_submit_reservations()
        return {
            "token_id": self.token_id,
            "running": self.is_running,
            "busy": self.is_busy,
            "pid": self.process.pid if self.process and self.process.poll() is None else None,
            "profile": self.profile_dir.name,
            "proxy_source": self.proxy_binding.source if self.proxy_binding else None,
            "solve_count": self.solve_count,
            "profile_solve_count": self.profile_solve_count,
            "profile_reset_count": self.profile_reset_count,
            "profile_reset_pending": self._profile_reset_pending,
            "webdriver": (
                self.last_fingerprint.get("webdriver")
                if self.last_fingerprint
                else None
            ),
            "last_error": self.last_error,
            "last_upstream_error": self.last_upstream_error,
            "video_submit_reservations": len(self._video_submit_reservations),
            "idle_seconds": 0 if self.is_busy else int(max(0, time.monotonic() - self.last_used_at)),
        }


class BrowserCaptchaService:
    _instance: Optional["BrowserCaptchaService"] = None
    _instance_lock = asyncio.Lock()

    def __init__(self, db):
        self.db = db
        self.website_key = FLOW_WEBSITE_KEY
        self._workers: Dict[int, NativeCdpAccountBrowser] = {}
        self._proxy_risk_states: Dict[str, ProxyRiskState] = {}
        self._risk_history_loaded = False
        self._risk_history_lock = asyncio.Lock()
        self._capacity_lock = asyncio.Lock()
        self._capacity_condition = asyncio.Condition()
        self._queued = 0
        self._closed = False
        self._reaper_task = asyncio.create_task(self._idle_reaper())

    @classmethod
    async def get_instance(cls, db=None) -> "BrowserCaptchaService":
        async with cls._instance_lock:
            if cls._instance is None:
                if db is None:
                    raise RuntimeError("native_cdp service requires a database")
                cls._instance = cls(db)
            elif db is not None:
                cls._instance.db = db
            return cls._instance

    def _browser_limit(self) -> int:
        return max(1, min(20, int(getattr(config, "browser_count", 1) or 1)))

    def _idle_ttl(self) -> int:
        value = getattr(config, "native_cdp_idle_ttl_seconds", DEFAULT_IDLE_TTL_SECONDS)
        try:
            return max(60, int(value))
        except Exception:
            return DEFAULT_IDLE_TTL_SECONDS

    @staticmethod
    def _risk_backoff_seconds(failure_streak: int) -> float:
        """Escalate repeated proxy verdicts from minutes to a six-hour quarantine."""
        base = float(getattr(config, "flow_traffic_cooldown_seconds", 120) or 0)
        if base <= 0:
            return 0.0
        steps = (
            base,
            max(base, 300.0),
            max(base, 900.0),
            max(base, 1800.0),
            max(base, 3600.0),
            max(base, 7200.0),
            max(base, 21600.0),
        )
        index = min(max(1, int(failure_streak)) - 1, len(steps) - 1)
        return steps[index]

    @staticmethod
    def _public_proxy_state(
        proxy_key: str,
        state: Optional[ProxyRiskState],
    ) -> Dict[str, Any]:
        now = time.time()
        if state is None:
            return {
                "proxy_key": proxy_key,
                "proxy_fingerprint": proxy_key[:12],
                "available": True,
                "failure_streak": 0,
                "cooldown_remaining_seconds": 0,
                "cooldown_until": None,
                "last_failure_at": None,
                "last_success_at": None,
                "token_ids": [],
            }
        remaining = max(0.0, state.cooldown_until - now)

        def iso_timestamp(value: Optional[float]) -> Optional[str]:
            if value is None:
                return None
            return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()

        return {
            "proxy_key": proxy_key,
            "proxy_fingerprint": proxy_key[:12],
            "available": remaining <= 0,
            "failure_streak": max(0, int(state.failure_streak)),
            "cooldown_remaining_seconds": int(remaining + 0.999),
            "cooldown_until": iso_timestamp(state.cooldown_until) if remaining > 0 else None,
            "last_failure_at": iso_timestamp(state.last_failure_at),
            "last_success_at": iso_timestamp(state.last_success_at),
            "token_ids": sorted(state.token_ids),
        }

    async def _resolve_proxy_url(
        self,
        token_id: int,
        token_proxy_url: Optional[str] = None,
    ) -> str:
        proxy_url = str(token_proxy_url or "").strip()
        if not proxy_url:
            token = await self.db.get_token(int(token_id))
            proxy_url = str(
                getattr(token, "captcha_proxy_url", "") or ""
            ).strip() if token else ""
        if proxy_url:
            _parse_proxy_url(proxy_url)
            return proxy_url

        captcha_config = await self.db.get_captcha_config()
        global_proxy = str(
            getattr(captcha_config, "browser_proxy_url", "") or ""
        ).strip()
        if bool(getattr(captcha_config, "browser_proxy_enabled", False)) and global_proxy:
            _parse_proxy_url(global_proxy)
            return global_proxy
        raise RuntimeError(
            f"native_cdp token {int(token_id)} has no token proxy and no enabled global browser proxy"
        )

    async def get_video_proxy_state(
        self,
        token_id: int,
        *,
        token_proxy_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        proxy_url = await self._resolve_proxy_url(token_id, token_proxy_url)
        proxy_key = _proxy_egress_key(proxy_url)
        state = self._proxy_risk_states.get(proxy_key)
        if state is not None:
            state.token_ids.add(int(token_id))
        return self._public_proxy_state(proxy_key, state)

    async def _record_proxy_risk(
        self,
        token_id: int,
        error: Any,
        *,
        token_proxy_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        proxy_url = await self._resolve_proxy_url(token_id, token_proxy_url)
        proxy_key = _proxy_egress_key(proxy_url)
        state = self._proxy_risk_states.setdefault(proxy_key, ProxyRiskState())
        now = time.time()
        monotonic_now = time.monotonic()
        token_key = int(token_id)
        state.token_ids.add(token_key)

        # Browser fetch and its outer error reporter can observe the same response.
        # Count it once so one rejection advances only one backoff step.
        if (
            state.last_failure_token_id == token_key
            and monotonic_now - state.last_failure_monotonic < 5.0
        ):
            return self._public_proxy_state(proxy_key, state)

        if state.last_failure_at is None or now - state.last_failure_at > 6 * 3600:
            state.failure_streak = 1
        else:
            state.failure_streak = max(0, int(state.failure_streak)) + 1
        cooldown_seconds = self._risk_backoff_seconds(state.failure_streak)
        state.cooldown_until = max(state.cooldown_until, now + cooldown_seconds)
        state.last_failure_at = now
        state.last_failure_token_id = token_key
        state.last_failure_monotonic = monotonic_now
        state.last_error = str(error or "traffic_control")[:240]
        public_state = self._public_proxy_state(proxy_key, state)
        debug_logger.log_warning(
            f"[NativeCDP] proxy risk fp={public_state['proxy_fingerprint']}, "
            f"token={token_key}, streak={state.failure_streak}, "
            f"cooldown={public_state['cooldown_remaining_seconds']}s"
        )
        return public_state

    async def _record_proxy_success(
        self,
        token_id: int,
        *,
        token_proxy_url: Optional[str] = None,
    ) -> None:
        proxy_url = await self._resolve_proxy_url(token_id, token_proxy_url)
        proxy_key = _proxy_egress_key(proxy_url)
        state = self._proxy_risk_states.get(proxy_key)
        if state is None:
            return
        state.token_ids.add(int(token_id))
        state.failure_streak = 0
        state.cooldown_until = 0.0
        state.last_success_at = time.time()
        state.last_error = ""
        debug_logger.log_info(
            f"[NativeCDP] proxy recovered fp={proxy_key[:12]}, token={int(token_id)}"
        )

    async def hydrate_proxy_risk_history(self, active_tokens: list[Any]) -> None:
        """Restore recent proxy risk from persistent request logs after restart."""
        if self._risk_history_loaded:
            return
        async with self._risk_history_lock:
            if self._risk_history_loaded:
                return
            self._risk_history_loaded = True
            get_logs = getattr(self.db, "get_logs", None)
            if not callable(get_logs):
                return

            token_proxy_keys: Dict[int, str] = {}
            global_proxy = ""
            try:
                captcha_config = await self.db.get_captcha_config()
                if bool(getattr(captcha_config, "browser_proxy_enabled", False)):
                    global_proxy = str(
                        getattr(captcha_config, "browser_proxy_url", "") or ""
                    ).strip()
            except Exception:
                global_proxy = ""

            for token in active_tokens:
                token_id = getattr(token, "id", None)
                if token_id is None:
                    continue
                proxy_url = str(
                    getattr(token, "captcha_proxy_url", "") or global_proxy
                ).strip()
                if not proxy_url:
                    continue
                try:
                    token_proxy_keys[int(token_id)] = _proxy_egress_key(proxy_url)
                except Exception:
                    continue

            try:
                logs = await get_logs(limit=500, include_payload=False)
            except TypeError:
                logs = await get_logs(limit=500)
            except Exception as exc:
                debug_logger.log_warning(
                    f"[NativeCDP] unable to hydrate proxy risk history: {exc}"
                )
                return

            now = time.time()
            cutoff = now - 6 * 3600
            events = []
            for log in logs or []:
                if str(log.get("operation") or "") != "generate_video":
                    continue
                token_id = log.get("token_id")
                try:
                    token_key = int(token_id)
                    status_code = int(log.get("status_code") or 0)
                except (TypeError, ValueError):
                    continue
                proxy_key = token_proxy_keys.get(token_key)
                if not proxy_key or (status_code != 429 and not 200 <= status_code < 300):
                    continue
                occurred_at = _timestamp_seconds(
                    log.get("updated_at") or log.get("created_at")
                )
                if occurred_at is None or occurred_at < cutoff:
                    continue
                events.append((occurred_at, token_key, proxy_key, status_code))

            for occurred_at, token_id, proxy_key, status_code in sorted(events):
                state = self._proxy_risk_states.get(proxy_key)
                if status_code == 429:
                    if state is None:
                        state = ProxyRiskState()
                        self._proxy_risk_states[proxy_key] = state
                    if (
                        state.last_failure_at is None
                        or occurred_at - state.last_failure_at > 6 * 3600
                    ):
                        state.failure_streak = 1
                    else:
                        state.failure_streak += 1
                    state.last_failure_at = occurred_at
                    state.token_ids.add(token_id)
                    state.cooldown_until = max(
                        state.cooldown_until,
                        occurred_at + self._risk_backoff_seconds(state.failure_streak),
                    )
                elif state is not None:
                    state.token_ids.add(token_id)
                    state.failure_streak = 0
                    state.cooldown_until = 0.0
                    state.last_success_at = occurred_at

            active_groups = sum(
                1
                for state in self._proxy_risk_states.values()
                if state.cooldown_until > now
            )
            if self._proxy_risk_states:
                debug_logger.log_info(
                    f"[NativeCDP] hydrated {len(self._proxy_risk_states)} proxy risk group(s), "
                    f"active_cooldowns={active_groups}"
                )

    def _running_workers(self) -> list[NativeCdpAccountBrowser]:
        return [worker for worker in self._workers.values() if worker.is_running]

    async def _ensure_capacity(self, worker: NativeCdpAccountBrowser) -> None:
        queued = False
        try:
            while not worker.is_running:
                async with self._capacity_lock:
                    running = self._running_workers()
                    if len(running) < self._browser_limit():
                        await worker.start()
                        return
                    idle_candidates = [
                        candidate
                        for candidate in running
                        if candidate.token_id != worker.token_id and not candidate.is_busy
                    ]
                    if idle_candidates:
                        victim = min(idle_candidates, key=lambda item: item.last_used_at)
                        await victim.stop(reason=f"capacity_for_token_{worker.token_id}")
                        continue
                if not queued:
                    queued = True
                    self._queued += 1
                    debug_logger.log_info(
                        f"[NativeCDP] token={worker.token_id} waiting for browser capacity "
                        f"(running={len(self._running_workers())}, limit={self._browser_limit()})"
                    )
                async with self._capacity_condition:
                    try:
                        await asyncio.wait_for(self._capacity_condition.wait(), timeout=1)
                    except asyncio.TimeoutError:
                        pass
        finally:
            if queued:
                self._queued = max(0, self._queued - 1)

    async def verify_session(self, token_id: int, project_id: str) -> None:
        """Use the production profile and proxy without solving or generating."""
        if self._closed:
            raise RuntimeError("native_cdp service is closed")
        worker = self._workers.get(int(token_id))
        if worker is None:
            worker = NativeCdpAccountBrowser(int(token_id), self.db)
            self._workers[int(token_id)] = worker
        worker.busy_count += 1
        try:
            await self._ensure_capacity(worker)
            await worker.verify_session(project_id)
        finally:
            worker.busy_count = max(0, worker.busy_count - 1)
            worker.last_used_at = time.monotonic()
            async with self._capacity_condition:
                self._capacity_condition.notify_all()

    async def flow_account_snapshot(self, token_id: int, expected_email: str) -> Dict[str, Any]:
        if self._closed:
            raise RuntimeError("native_cdp service is closed")
        worker = self._workers.get(int(token_id))
        if worker is None:
            worker = NativeCdpAccountBrowser(int(token_id), self.db)
            self._workers[int(token_id)] = worker
        worker.busy_count += 1
        try:
            await self._ensure_capacity(worker)
            return await worker.flow_account_snapshot(expected_email)
        finally:
            worker.busy_count = max(0, worker.busy_count - 1)
            worker.last_used_at = time.monotonic()
            async with self._capacity_condition:
                self._capacity_condition.notify_all()

    async def verify_flow_import(self, cookies: str, proxy_url: str, expected_email: str = "") -> Dict[str, Any]:
        """Verify before any account write; never seed an untrusted identity into an existing profile."""
        if self._closed:
            raise RuntimeError("native_cdp service is closed")
        if -1 in self._workers:
            raise NativeSessionError("flow_account_busy", protocol="angular")
        candidate = SimpleNamespace(st="", auth_mode="flow", google_cookies=cookies, captcha_proxy_url=proxy_url)
        async def get_token(_):
            return candidate
        root = _profile_root()
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="sync-check-", dir=root) as folder:
            worker = NativeCdpAccountBrowser(-1, SimpleNamespace(get_token=get_token))
            worker.profile_dir = Path(folder)
            worker._profile_reset_pending = False
            worker.busy_count = 1
            self._workers[-1] = worker
            try:
                async def verify():
                    await self._ensure_capacity(worker)
                    return await worker.flow_account_snapshot(expected_email)
                return await asyncio.wait_for(verify(), timeout=75)
            finally:
                try:
                    await worker.stop(reason="flow_import_verification_complete")
                finally:
                    self._workers.pop(-1, None)
                    async with self._capacity_condition:
                        self._capacity_condition.notify_all()

    async def get_token(
        self,
        project_id: str,
        action: str = "IMAGE_GENERATION",
        token_id: Optional[int] = None,
        page_protocol: str = "labs",
    ) -> tuple[Optional[str], Optional[str]]:
        if self._closed:
            raise RuntimeError("native_cdp service is closed")
        if not token_id:
            raise RuntimeError("native_cdp requires token_id")
        token_key = int(token_id)
        worker = self._workers.get(token_key)
        if worker is None:
            worker = NativeCdpAccountBrowser(token_key, self.db)
            self._workers[token_key] = worker
        worker.busy_count += 1
        try:
            await self._ensure_capacity(worker)
            token = await worker.solve(project_id, action, website_key=self.website_key, page_protocol=page_protocol)
            if token and str(action or "").strip().upper() == "VIDEO_GENERATION":
                worker.reserve_for_video_submit()
            return token, f"native:{token_key}" if token else None
        finally:
            worker.busy_count = max(0, worker.busy_count - 1)
            worker.last_used_at = time.monotonic()
            async with self._capacity_condition:
                self._capacity_condition.notify_all()

    async def fetch_json(
        self,
        *,
        token_id: Optional[int],
        project_id: str,
        url: str,
        method: str = "POST",
        headers: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None,
        timeout: int = 60,
        consume_video_reservation: bool = False,
        page_protocol: Optional[str] = None,
    ) -> Dict[str, Any]:
        if self._closed:
            raise RuntimeError("native_cdp service is closed")
        if not token_id:
            raise RuntimeError("native_cdp browser fetch requires token_id")
        token_key = int(token_id)
        worker = self._workers.get(token_key)
        if worker is None:
            worker = NativeCdpAccountBrowser(token_key, self.db)
            self._workers[token_key] = worker
        worker.busy_count += 1
        if consume_video_reservation:
            worker.consume_video_submit_reservation()
        tracks_proxy_risk = consume_video_reservation or (
            url == FLOW_RPC_URL and (json_data or {}).get("rpc_id") in {"MZZa6b", "jIps6", "ogiZ0b"}
        )
        try:
            await self._ensure_capacity(worker)
            result = await worker.fetch_json(
                project_id=project_id,
                url=url,
                method=method,
                headers=headers,
                json_data=json_data,
                timeout=timeout,
                page_protocol=page_protocol,
            )
            if tracks_proxy_risk:
                try:
                    await self._record_proxy_success(
                        token_key,
                        token_proxy_url=(
                            worker.proxy_binding.url
                            if worker.proxy_binding is not None
                            else None
                        ),
                    )
                except Exception as exc:
                    debug_logger.log_warning(
                        f"[NativeCDP] unable to record proxy success token={token_key}: {exc}"
                    )
            return result
        except Exception as exc:
            if tracks_proxy_risk and is_media_traffic_error(exc):
                try:
                    await self._record_proxy_risk(
                        token_key,
                        exc,
                        token_proxy_url=(
                            worker.proxy_binding.url
                            if worker.proxy_binding is not None
                            else None
                        ),
                    )
                except Exception as risk_exc:
                    debug_logger.log_warning(
                        f"[NativeCDP] unable to record proxy risk token={token_key}: {risk_exc}"
                    )
            raise
        finally:
            worker.busy_count = max(0, worker.busy_count - 1)
            worker.last_used_at = time.monotonic()
            async with self._capacity_condition:
                self._capacity_condition.notify_all()

    def get_fingerprint(self, token_id: Optional[int]) -> Optional[Dict[str, Any]]:
        if not token_id:
            return None
        worker = self._workers.get(int(token_id))
        return dict(worker.last_fingerprint) if worker and worker.last_fingerprint else None

    async def report_flow_error(
        self,
        project_id: Optional[str] = None,
        *,
        token_id: Optional[int] = None,
        error_reason: str = "",
        error_message: str = "",
    ) -> None:
        if token_id and int(token_id) in self._workers:
            worker = self._workers[int(token_id)]
            worker.last_upstream_error = (
                error_message or error_reason or "upstream_error"
            )[:240]
        reported_error = error_message or error_reason or ""
        if token_id and is_media_traffic_error(reported_error):
            try:
                await self._record_proxy_risk(int(token_id), reported_error)
            except Exception as exc:
                debug_logger.log_warning(
                    f"[NativeCDP] unable to record reported proxy risk token={token_id}: {exc}"
                )
        debug_logger.log_warning(
            f"[NativeCDP] upstream error project_id={project_id or '-'}, "
            f"token_id={token_id or '-'}, reason={(error_reason or error_message or 'unknown')[:160]}"
        )

    async def remove_token(self, token_id: int) -> None:
        worker = self._workers.pop(int(token_id), None)
        if worker:
            await worker.delete_profile()
        async with self._capacity_condition:
            self._capacity_condition.notify_all()

    async def reload_config(self) -> None:
        async with self._capacity_lock:
            running = sorted(
                self._running_workers(),
                key=lambda worker: worker.last_used_at,
            )
            while len(running) > self._browser_limit():
                victim = next((worker for worker in running if not worker.is_busy), None)
                if victim is None:
                    break
                await victim.stop(reason="config_limit_reduced")
                running.remove(victim)
        async with self._capacity_condition:
            self._capacity_condition.notify_all()

    async def warmup_active_tokens(self) -> list[Dict[str, Any]]:
        active_tokens = await self.db.get_active_tokens()
        await self.hydrate_proxy_risk_history(active_tokens)
        results = []
        for token in active_tokens[: self._browser_limit()]:
            worker = self._workers.get(int(token.id))
            if worker is None:
                worker = NativeCdpAccountBrowser(int(token.id), self.db)
                self._workers[int(token.id)] = worker
            try:
                await self._ensure_capacity(worker)
                results.append({"token_id": token.id, "success": True})
            except Exception as exc:
                results.append(
                    {
                        "token_id": token.id,
                        "success": False,
                        "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                    }
                )
        return results

    async def _idle_reaper(self) -> None:
        try:
            while not self._closed:
                await asyncio.sleep(10)
                now = time.monotonic()
                ttl = self._idle_ttl()
                for worker in list(self._workers.values()):
                    if worker.is_running and not worker.is_busy and now - worker.last_used_at >= ttl:
                        await worker.stop(reason="idle_ttl")
                async with self._capacity_condition:
                    self._capacity_condition.notify_all()
        except asyncio.CancelledError:
            pass

    def get_status(self) -> Dict[str, Any]:
        risk_groups = [
            self._public_proxy_state(proxy_key, state)
            for proxy_key, state in self._proxy_risk_states.items()
        ]
        for item in risk_groups:
            item.pop("proxy_key", None)
        risk_groups.sort(
            key=lambda item: (
                not item["available"],
                item["failure_streak"],
                item["last_failure_at"] or "",
            ),
            reverse=True,
        )
        return {
            "mode": "native_cdp",
            "browser_limit": self._browser_limit(),
            "idle_ttl_seconds": self._idle_ttl(),
            "running": len(self._running_workers()),
            "queued": self._queued,
            "proxy_risk_history_loaded": self._risk_history_loaded,
            "video_proxy_risk_groups": risk_groups,
            "workers": [
                worker.status()
                for worker in sorted(self._workers.values(), key=lambda item: item.token_id)
            ],
        }

    async def close(self) -> None:
        self._closed = True
        if self._reaper_task:
            self._reaper_task.cancel()
            try:
                await self._reaper_task
            except asyncio.CancelledError:
                pass
        await asyncio.gather(
            *(worker.stop(reason="service_shutdown") for worker in self._workers.values()),
            return_exceptions=True,
        )
        self._workers.clear()
        async with self._capacity_condition:
            self._capacity_condition.notify_all()
        type(self)._instance = None
