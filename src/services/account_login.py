"""Interactive login into the exact persistent profile used by Native CDP.

No source browser, cookie transfer, separate account database or password store.
Only one admin login lease is allowed, on an isolated display. The account is
disabled durably until identity + real Flow RPCs have passed after browser close.
"""
import asyncio
import hashlib
import os
import secrets
import shutil
import socket
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from ..core.config import config
from ..core.generation_errors import NativeSessionError
from ..core.logger import debug_logger
from ..core.native_session_state import claim_local_session, validate_local_session_proxy, local_session_state
from .browser_captcha_native_cdp import BrowserCaptchaService, NativeCdpAccountBrowser


class LoginError(Exception):
    def __init__(self, message, status=409):
        super().__init__(message)
        self.status = status


class LoginDesktop:
    """Private X server and loopback-only, ephemeral-password VNC transport."""
    def __init__(self):
        self.processes = []
        self.folder = None
        self.display = None
        self.port = None
        self.password = None

    @staticmethod
    def available():
        return (os.name == 'posix' and all(shutil.which(cmd) for cmd in ('Xvfb', 'x11vnc', 'fluxbox'))
                and Path('/usr/share/novnc/core/rfb.js').is_file())

    async def _spawn(self, *args, **kwargs):
        task = asyncio.create_task(asyncio.create_subprocess_exec(
            *args, stderr=asyncio.subprocess.DEVNULL, **kwargs))
        try:
            process = await asyncio.shield(task)
        except asyncio.CancelledError:
            # Process creation can finish after request cancellation. Own it so
            # the enclosing manager's cleanup still reaps the child.
            self.processes.append(await task)
            raise
        self.processes.append(process)
        return process

    async def start(self):
        if not self.available():
            raise LoginError('账号登录需要新版 headed 镜像中的 Xvfb、x11vnc 和 noVNC', 503)
        self.folder = tempfile.TemporaryDirectory(prefix='flow2api-login-')
        self.password = secrets.token_urlsafe(6)  # RFB password is exactly 8 ASCII chars.
        password_file = Path(self.folder.name) / 'vnc-password'
        with password_file.open('w', encoding='ascii') as stream:
            os.chmod(password_file, 0o600)
            stream.write(self.password + '\n')
        xvfb = await self._spawn('Xvfb', '-displayfd', '1', '-screen', '0', '1440x900x24',
                                 '-nolisten', 'tcp', '-ac', stdout=asyncio.subprocess.PIPE)
        number = (await asyncio.wait_for(xvfb.stdout.readline(), 10)).decode().strip()
        if not number.isdigit() or xvfb.returncode is not None:
            raise LoginError('登录桌面启动失败', 503)
        self.display = ':' + number
        await self._spawn('fluxbox', env={**os.environ, 'DISPLAY': self.display},
                          stdout=asyncio.subprocess.DEVNULL)
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            self.port = listener.getsockname()[1]
        vnc = await self._spawn('x11vnc', '-display', self.display, '-rfbport', str(self.port),
                               '-listen', '127.0.0.1', '-localhost', '-no6', '-forever', '-shared',
                               '-passwdfile', str(password_file), '-norc', '-noremote',
                               '-nocmds', '-novncconnect', '-quiet', stdout=asyncio.subprocess.DEVNULL)
        for _ in range(50):
            if vnc.returncode is not None:
                break
            writer = None
            try:
                reader, writer = await asyncio.wait_for(asyncio.open_connection('127.0.0.1', self.port), 1)
                banner = await asyncio.wait_for(reader.readexactly(12), 1)
                if banner.startswith(b'RFB '):
                    return
            except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError):
                pass
            finally:
                # A partial banner or cancellation must not leave a handshake
                # socket open while the next readiness attempt connects.
                if writer:
                    writer.close()
                    try:
                        await asyncio.wait_for(writer.wait_closed(), 1)
                    except (OSError, asyncio.TimeoutError):
                        pass
            await asyncio.sleep(.1)
        raise LoginError('登录远程画面启动失败', 503)

    def alive(self):
        return bool(self.processes) and all(p.returncode is None for p in self.processes)

    async def close(self):
        for process in reversed(self.processes):
            if process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), 3)
                except asyncio.TimeoutError:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    await process.wait()
            else:
                await process.wait()
        self.processes.clear()
        if self.folder:
            self.folder.cleanup()
            self.folder = None
        self.password = None
        self.port = None


class AccountLoginManager:
    LEASE_SECONDS = 20 * 60

    def __init__(self, db, token_manager, concurrency_manager, valid_admin, load_balancer=None):
        self.db, self.tokens, self.concurrency = db, token_manager, concurrency_manager
        self.valid_admin = valid_admin
        self.load_balancer = load_balancer
        self.active = None
        self._lock = asyncio.Lock()
        self._watchdog = None
        self._maintenance = None
        self._closed = False

    def start_maintenance(self):
        if not self._maintenance or self._maintenance.done():
            self._maintenance = asyncio.create_task(self._maintain(), name='native-account-health')

    async def check_accounts(self):
        """Low-cost checks, same profile and shared verification cache as generation."""
        if config.captcha_method != 'native_cdp':
            return
        try:
            interval = max(60, min(3600, int(os.environ.get('NATIVE_ACCOUNT_CHECK_INTERVAL_SECONDS', '300'))))
        except ValueError:
            interval = 300
        service = await BrowserCaptchaService.get_instance(self.db)
        for token in await self.db.get_active_tokens():
            if self._closed:
                return
            if token.auth_mode != 'flow':
                continue
            try:
                owned = local_session_state(token.id)
            except NativeSessionError as exc:
                self.tokens.native_sessions.reject(token, exc)
                continue
            if not owned:
                continue
            if self.metadata(token.id)['login_in_progress'] or not self.tokens.native_sessions.available(token):
                continue
            worker = service._workers.get(token.id)
            if worker and worker.is_busy:
                continue
            status = self.tokens.native_sessions.status(token)
            checked = status.get('session_checked_at')
            if checked and (datetime.now(timezone.utc) - datetime.fromisoformat(checked)).total_seconds() < interval:
                continue
            # Recheck after earlier accounts' awaits; never revive manually disabled accounts.
            current = await self.db.get_token(token.id)
            if not current or not current.is_active or self.metadata(token.id)['login_in_progress']:
                continue
            try:
                await asyncio.wait_for(self.tokens._refresh_credits_with_status(token.id), 75)
            except asyncio.CancelledError:
                # Deleting an account may cancel its shared probe, not this
                # maintenance loop. Shutdown cancellation must still propagate.
                if asyncio.current_task().cancelling():
                    raise
            except asyncio.TimeoutError:
                self.tokens.native_sessions.reject(current)

    async def _maintain(self):
        while not self._closed:
            try:
                await self.check_accounts()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                debug_logger.log_warning(f'[AccountLogin] health round failed: {type(exc).__name__}')
            await asyncio.sleep(30)

    def metadata(self, token_id):
        active = self.active
        return {'login_in_progress': bool(active and active['token_id'] == token_id),
                'login_phase': active['phase'] if active and active['token_id'] == token_id else None}

    def status(self, owner):
        active = self.active
        return {'available': config.captcha_method == 'native_cdp' and LoginDesktop.available(),
                'active': ({'token_id': active['token_id'], 'session_id': active['id'],
                            'phase': active['phase'], 'owned': active['owner'] == owner,
                            'remaining_seconds': max(0, int(active['deadline'] - time.monotonic()))}
                           if active else None)}

    def viewer(self, ticket):
        active = self.active
        if (not active or active['phase'] != 'login' or not ticket
                or not secrets.compare_digest(active['ticket'], ticket)
                or time.monotonic() >= active['deadline'] or not self.valid_admin(active['owner'])
                or not active['desktop'].alive()):
            return None
        return active

    def _owned(self, session_id, owner):
        active = self.active
        if not active or active['id'] != session_id or active['owner'] != owner:
            raise LoginError('登录窗口已结束或由其他管理员持有')
        if time.monotonic() >= active['deadline'] or not self.valid_admin(owner):
            raise LoginError('登录窗口已过期，请重新打开', 401)
        return active

    async def start(self, token_id, owner):
        async with self._lock:
            if self._closed or config.captcha_method != 'native_cdp' or not LoginDesktop.available():
                raise LoginError('请使用新版 headed 镜像并启用 native_cdp', 503)
            if self.active:
                raise LoginError('已有账号正在登录，请先完成或结束该窗口')
            token = await self.db.get_token(token_id)
            if not token:
                raise LoginError('账号不存在', 404)
            if not str(token.captcha_proxy_url or '').strip():
                raise LoginError('请先为此账号配置独立代理地址，登录与生成使用同一出口', 400)
            # Serialize against the existing import path through its entire identity check/write.
            if self.tokens._flow_sync_lock.locked():
                raise LoginError('账号同步正在进行，请稍后重试')
            async with self.tokens._flow_sync_lock:
                service = await BrowserCaptchaService.get_instance(self.db)
                worker = service._workers.get(token_id)
                if worker is None:
                    worker = NativeCdpAccountBrowser(token_id, self.db)
                    service._workers[token_id] = worker
                if self.load_balancer and not await self.load_balancer.pause_for_login(token_id):
                    raise LoginError('账号已有分配的生成任务，请等待完成后登录')
                try:
                    if not await self.concurrency.pause_for_login(token_id):
                        raise LoginError('账号有执行中的生成任务，请等待完成后登录')
                except BaseException:
                    if self.load_balancer:
                        await self.load_balancer.resume_after_login(token_id)
                    raise
                if worker.is_busy:
                    await self.concurrency.resume_after_login(token_id)
                    if self.load_balancer:
                        await self.load_balancer.resume_after_login(token_id)
                    raise LoginError('账号正在验证或执行请求，请稍后重试')
                # No await between the idle check and reservation. All CDP entrypoints check it.
                worker.interactive_login = True
                active = self.active = {'id': secrets.token_urlsafe(18), 'token_id': token_id, 'owner': owner,
                    'ticket': secrets.token_urlsafe(32), 'deadline': time.monotonic() + self.LEASE_SECONDS,
                    'phase': 'starting', 'worker': worker, 'desktop': LoginDesktop(), 'service': service}
                try:
                    binding = await worker._resolve_proxy()
                    validate_local_session_proxy(local_session_state(token_id, worker.profile_dir), binding.url)
                    await worker.stop(reason='interactive_login_start')
                    claim_local_session(token_id, binding.url, worker.profile_dir)
                    # Ownership persists across abort/restart; no auto-enable from external sync.
                    await self.db.update_token(token_id, auth_mode='flow', is_active=False, at=None, at_expires=None,
                        ban_reason=None, banned_at=None,
                        st='flow:' + hashlib.sha256(token.email.strip().lower().encode()).hexdigest(), google_cookies=None)
                    self.tokens.native_sessions.discard(token_id)
                    self.tokens._flow_verified.pop(token_id, None)
                    await active['desktop'].start()
                    worker.display_override = active['desktop'].display
                    await asyncio.wait_for(service._ensure_capacity(worker), 45)
                    worker._profile_reset_pending = False
                    worker._profile_reset_reason = ''
                    target, session = await worker._create_page_session()
                    await worker.connection.send('Page.navigate', {'url': 'https://flow.google.com/'}, session_id=session)
                    await worker.connection.send('Target.activateTarget', {'targetId': target})
                    active['phase'] = 'login'
                    if not self._watchdog or self._watchdog.done():
                        self._watchdog = asyncio.create_task(self._watch())
                    return self.status(owner)
                except BaseException as exc:
                    debug_logger.log_runtime_event('account_login_failed', token_id=token_id, stage='login_start',
                        reason=exc.reason if isinstance(exc, NativeSessionError) else type(exc).__name__)
                    await self._cleanup_shielded(active)
                    raise

    async def finish(self, session_id, owner):
        async with self._lock:
            active = self._owned(session_id, owner)
            active['phase'] = 'verifying'  # Immediately revoke viewer access, before any probe.
            worker, token_id = active['worker'], active['token_id']
            try:
                await worker.stop(reason='interactive_login_save')
                await active['desktop'].close()
                worker.display_override = None
                token = await self.db.get_token(token_id)
                if not token:
                    raise LoginError('账号不存在', 404)
                async def verify():
                    await active['service']._ensure_capacity(worker)
                    return await worker.flow_account_snapshot(token.email)
                snapshot = await asyncio.wait_for(verify(), 75)
                project = snapshot['projects'][0] if snapshot['projects'] else {}
                await self.db.update_token(token_id, credits=snapshot['credits'],
                    user_paygate_tier=snapshot['userPaygateTier'], current_project_id=project.get('project_id'),
                    current_project_name=project.get('project_name'))
                await self.tokens.enable_token(token_id)
                current = await self.db.get_token(token_id)
                self.tokens.native_sessions.verified(current)
                self.tokens._flow_verified[token_id] = (self.tokens.native_sessions._revision(current), time.monotonic())
                debug_logger.log_runtime_event('account_login_verified', token_id=token_id, stage='login_complete')
                return {'success': True, 'token_id': token_id, 'session_status': 'verified', 'credits': snapshot['credits']}
            except Exception as exc:
                debug_logger.log_runtime_event('account_login_failed', token_id=token_id, stage='login_verification',
                    reason=exc.reason if isinstance(exc, NativeSessionError) else type(exc).__name__)
                token = await self.db.get_token(token_id)
                if token:
                    self.tokens.native_sessions.reject(token, exc)
                raise
            finally:
                await self._cleanup_shielded(active, keep_browser=True)

    async def cancel(self, session_id, owner):
        async with self._lock:
            active = self._owned(session_id, owner)
            await self._cleanup_shielded(active)

    async def _cleanup(self, active, keep_browser=False):
        active['phase'] = 'closing'
        worker = active['worker']
        try:
            if not keep_browser or worker.display_override:
                await worker.stop(reason='interactive_login_end')
        finally:
            try:
                await active['desktop'].close()
            finally:
                worker.display_override = None
                worker.interactive_login = False
                worker.last_used_at = time.monotonic()
                await self.concurrency.resume_after_login(active['token_id'])
                if self.load_balancer:
                    await self.load_balancer.resume_after_login(active['token_id'])
                if self.active is active:
                    self.active = None

    async def _cleanup_shielded(self, active, keep_browser=False):
        task = asyncio.create_task(self._cleanup(active, keep_browser))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.shield(task)
            raise

    async def _watch(self):
        while not self._closed:
            await asyncio.sleep(2)
            async with self._lock:
                active = self.active
                if not active:
                    return
                if (time.monotonic() >= active['deadline'] or not self.valid_admin(active['owner'])
                        or not active['desktop'].alive() or not active['worker'].is_running):
                    await self._cleanup_shielded(active)

    async def close(self):
        self._closed = True
        if self._maintenance:
            self._maintenance.cancel()
            await asyncio.gather(self._maintenance, return_exceptions=True)
        if self._watchdog:
            self._watchdog.cancel()
            await asyncio.gather(self._watchdog, return_exceptions=True)
        async with self._lock:
            if self.active:
                await self._cleanup_shielded(self.active)
