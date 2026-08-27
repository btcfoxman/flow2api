"""Native CDP captcha service with one persistent Chromium profile per token."""

from __future__ import annotations

import asyncio
import json
import os
import platform
import random
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional
from urllib.parse import quote, unquote, urlparse

from ..core.config import config
from ..core.logger import debug_logger


FLOW_PROJECT_BASE_URL = "https://labs.google/fx/zh/tools/flow"
FLOW_WEBSITE_KEY = "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV"
DEFAULT_PROFILE_ROOT = Path("tmp") / "native_cdp_profiles"
DEFAULT_IDLE_TTL_SECONDS = 600
DEFAULT_VIDEO_SUBMIT_RESERVATION_SECONDS = 60


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
        self._project_sessions: Dict[str, tuple[str, str]] = {}
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
        if self.is_running and self.proxy_binding and self.proxy_binding.signature == proxy_binding.signature:
            return
        if self.process or self.connection:
            await self.stop(reason="proxy_changed_or_reconnect")

        executable = _detect_browser_executable()
        if not executable:
            raise RuntimeError(
                "native_cdp browser executable not found; configure BROWSER_EXECUTABLE_PATH "
                "or use the headed image"
            )

        self.profile_dir.mkdir(parents=True, exist_ok=True)
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
        self._video_submit_reservations.clear()
        if connection:
            try:
                await connection.send("Browser.close", timeout=3)
            except Exception:
                pass
            await connection.close()
        if process and process.poll() is None:
            process.terminate()
            try:
                await asyncio.wait_for(asyncio.to_thread(process.wait), timeout=8)
            except Exception:
                process.kill()
                try:
                    await asyncio.to_thread(process.wait)
                except Exception:
                    pass
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

    async def _seed_session_cookie(self, session_id: str) -> None:
        if not self.connection:
            return
        token = await self.db.get_token(self.token_id)
        session_token = str(getattr(token, "st", "") or "").strip() if token else ""
        if not session_token:
            return
        cookie_names = [
            "__Secure-next-auth.session-token",
            "__Secure-next-auth.session-token.0",
        ]
        for cookie_name in cookie_names:
            try:
                await self.connection.send(
                    "Network.setCookie",
                    {
                        "name": cookie_name,
                        "value": session_token,
                        "domain": ".google",
                        "path": "/",
                        "secure": True,
                        "httpOnly": True,
                        "sameSite": "None",
                    },
                    session_id=session_id,
                    timeout=5,
                )
            except Exception:
                pass

    @staticmethod
    def _project_page_url(project_id: Optional[str]) -> str:
        normalized_project_id = str(project_id or "").strip()
        if not normalized_project_id:
            return FLOW_PROJECT_BASE_URL
        return f"{FLOW_PROJECT_BASE_URL}/project/{quote(normalized_project_id, safe='')}"

    async def _wait_for_document_ready(self, session_id: str, timeout: float = 35) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                ready_state = await self._evaluate(
                    session_id,
                    "document.readyState",
                    timeout=3,
                )
                if ready_state in {"interactive", "complete"}:
                    return
            except Exception:
                pass
            await asyncio.sleep(0.25)
        raise TimeoutError("real Flow project page did not become ready")

    async def _open_real_project_page(
        self,
        session_id: str,
        project_id: str,
    ) -> None:
        if not self.connection:
            raise ConnectionError("native CDP browser is not connected")
        page_url = self._project_page_url(project_id)
        navigation = await self.connection.send(
            "Page.navigate",
            {"url": page_url},
            session_id=session_id,
            timeout=45,
        )
        if navigation.get("errorText"):
            raise CdpProtocolError(
                f"real Flow project page navigation failed: {navigation['errorText']}"
            )
        await self._wait_for_document_ready(session_id)
        current_url = str(
            await self._evaluate(session_id, "window.location.href", timeout=5) or ""
        )
        normalized_project_id = str(project_id or "").strip()
        if normalized_project_id and f"/project/{normalized_project_id}" not in current_url:
            raise RuntimeError(
                "real Flow project page did not retain the requested project context"
            )

    async def _get_or_create_project_session(
        self,
        project_id: str,
    ) -> tuple[str, str]:
        normalized_project_id = str(project_id or "").strip()
        cached = self._project_sessions.get(normalized_project_id)
        if cached:
            target_id, session_id = cached
            try:
                await self._evaluate(session_id, "document.readyState", timeout=3)
                await self._seed_session_cookie(session_id)
                return target_id, session_id
            except Exception:
                self._project_sessions.pop(normalized_project_id, None)

        target_id, session_id = await self._create_page_session()
        try:
            await self._seed_session_cookie(session_id)
            await self._open_real_project_page(session_id, normalized_project_id)
            await self._capture_fingerprint(session_id)
        except Exception:
            if self.connection and not self.connection.closed:
                try:
                    await self.connection.send(
                        "Target.closeTarget",
                        {"targetId": target_id},
                        timeout=5,
                    )
                except Exception:
                    pass
            raise
        self._project_sessions[normalized_project_id] = (target_id, session_id)
        return target_id, session_id

    async def _discard_project_session(self, project_id: Optional[str]) -> None:
        normalized_project_id = str(project_id or "").strip()
        cached = self._project_sessions.pop(normalized_project_id, None)
        if not cached or not self.connection or self.connection.closed:
            return
        target_id, _ = cached
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

    async def fetch_json(
        self,
        *,
        project_id: str,
        url: str,
        method: str = "POST",
        headers: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None,
        timeout: int = 60,
    ) -> Dict[str, Any]:
        """Execute an API request in the token's persistent real Flow project page."""
        async with self.solve_lock:
            self.busy_count += 1
            try:
                await self.start()
                _, session_id = await self._get_or_create_project_session(project_id)
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
                if "native browser fetch failed" in str(exc).lower():
                    await self._discard_project_session(project_id)
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
    ) -> Optional[str]:
        async with self.solve_lock:
            self.busy_count += 1
            try:
                await self.start()
                _, session_id = await self._get_or_create_project_session(project_id)
                await self._wait_for_recaptcha(session_id)
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
                await self._discard_project_session(project_id)
                if not self.is_running:
                    await self.stop(reason="runtime_disconnected")
                return None
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

    async def get_token(
        self,
        project_id: str,
        action: str = "IMAGE_GENERATION",
        token_id: Optional[int] = None,
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
            token = await worker.solve(project_id, action, website_key=self.website_key)
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
        try:
            await self._ensure_capacity(worker)
            return await worker.fetch_json(
                project_id=project_id,
                url=url,
                method=method,
                headers=headers,
                json_data=json_data,
                timeout=timeout,
            )
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
        return {
            "mode": "native_cdp",
            "browser_limit": self._browser_limit(),
            "idle_ttl_seconds": self._idle_ttl(),
            "running": len(self._running_workers()),
            "queued": self._queued,
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
