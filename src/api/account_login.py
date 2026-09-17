"""Admin-only account management and a same-origin, fixed-target VNC gateway."""
import asyncio
import hashlib
import time
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from . import admin
from ..core.config import config
from ..core.models import Token
from ..core.native_session_state import claim_local_session
from ..core.generation_errors import NativeSessionError
from ..services.account_login import LoginError

COOKIE = 'flow2api_login_viewer'
PREFIX = '/account-login'
NOVNC_ROOT = Path('/usr/share/novnc')
STATIC_ROOT = Path(__file__).resolve().parents[2] / 'static'
HEADERS = {'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
           'X-Content-Type-Options': 'nosniff', 'X-Frame-Options': 'SAMEORIGIN',
           'Content-Security-Policy': "frame-ancestors 'self'"}


def same_origin(request, required=False):
    origin = request.headers.get('origin')
    if not origin:
        return not required and request.headers.get('sec-fetch-site') != 'cross-site'
    source, target = urlsplit(origin), urlsplit(str(request.url))
    scheme = {'ws': 'http', 'wss': 'https'}.get(target.scheme, target.scheme)
    return source.scheme == scheme and source.netloc == target.netloc


def asset_path(asset):
    if '\\' in asset or any(p.startswith('.') for p in Path(asset).parts):
        raise HTTPException(404)
    root = NOVNC_ROOT.resolve()
    candidate = (root / asset).resolve()
    # Only noVNC's module dependencies. No demos, utility scripts or configs.
    if (not candidate.is_relative_to(root) or not candidate.is_file()
            or Path(asset).parts[0] not in {'core', 'vendor'}):
        raise HTTPException(404)
    return candidate


class NewAccount(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    captcha_proxy_url: str = Field(min_length=1, max_length=2048)
    remark: str = Field(default='', max_length=500)


def register_account_login(app, manager):
    async def auth(request: Request, token=Depends(admin.verify_admin_token)):
        if not same_origin(request):
            raise HTTPException(403, '不允许跨站访问账号登录')
        return token

    def response(data):
        return JSONResponse(data, headers=HEADERS)

    async def execute(operation):
        try:
            return await operation
        except LoginError as exc:
            raise HTTPException(exc.status, str(exc)) from None
        except NativeSessionError as exc:
            messages = {'flow_identity_mismatch': '登录账号与此账号邮箱不一致，请确认 Google 账号',
                        'local_session_proxy_changed': '账号代理与持久化登录 Profile 不一致，不能直接替换出口',
                        'local_session_state_invalid': '账号本地会话配置损坏，请检查持久化目录',
                        'flow_account_busy': '账号正在执行请求，请稍后重试'}
            raise HTTPException(409, messages.get(exc.reason,
                'Flow 会话验证未通过，账号保持暂停；请重新打开登录窗口完成认证后再验证')) from None
        except Exception:
            raise HTTPException(503, '登录服务暂时不可用，账号保持原有或暂停状态，请稍后重试') from None

    def viewer(request):
        if not same_origin(request):
            raise HTTPException(403)
        lease = manager.viewer(request.cookies.get(COOKIE))
        if not lease:
            raise HTTPException(401, '登录窗口已结束，请返回控制台重新打开')
        return lease

    @app.get('/api/account-login')
    async def status(token=Depends(auth)):
        return response(manager.status(token))

    @app.post('/api/accounts')
    async def create_account(body: NewAccount, token=Depends(auth)):
        if config.captcha_method != 'native_cdp':
            raise HTTPException(400, '账号登录需要 native_cdp 模式')
        email = body.email.strip().lower()
        if email.count('@') != 1 or any(c.isspace() for c in email) or not all(email.split('@')):
            raise HTTPException(400, '请输入有效的账号邮箱')
        proxy, _ = admin._normalize_plugin_captcha_proxy_url({'captcha_proxy_url': body.captcha_proxy_url})
        async with manager.tokens._flow_sync_lock:
            if any(str(t.email).strip().lower() == email for t in await manager.db.get_all_tokens()):
                raise HTTPException(409, '账号已存在，请使用该账号的“账号登录”入口')
            token_id = await manager.db.add_token(Token(
                st='flow:' + hashlib.sha256(email.encode()).hexdigest(), auth_mode='flow', email=email,
                name=email.split('@')[0], remark=body.remark, captcha_proxy_url=proxy, is_active=False))
            claim_local_session(token_id, proxy)
        return response({'success': True, 'token_id': token_id})

    @app.post('/api/accounts/{token_id}/login')
    async def start(token_id: int, token=Depends(auth)):
        return response(await execute(manager.start(token_id, token)))

    @app.post('/api/account-login/{session_id}/viewer')
    async def issue_viewer(session_id: str, request: Request, token=Depends(auth)):
        try:
            lease = manager._owned(session_id, token)
        except LoginError as exc:
            raise HTTPException(exc.status, str(exc)) from None
        if lease['phase'] != 'login':
            raise HTTPException(409, '登录窗口尚未就绪或正在关闭')
        result = response({'url': PREFIX + '/viewer'})
        result.set_cookie(COOKIE, lease['ticket'], httponly=True, secure=request.url.scheme == 'https',
                          samesite='strict', path=PREFIX, max_age=max(1, int(lease['deadline'] - time.monotonic())))
        return result

    @app.post('/api/account-login/{session_id}/finish')
    async def finish(session_id: str, token=Depends(auth)):
        return response(await execute(manager.finish(session_id, token)))

    @app.post('/api/account-login/{session_id}/cancel')
    async def cancel(session_id: str, token=Depends(auth)):
        await execute(manager.cancel(session_id, token))
        return response({'success': True})

    @app.get(PREFIX)
    async def page():
        # Like /manage: HTML has no credentials; all data/actions require admin auth.
        return FileResponse(STATIC_ROOT / 'account-login.html', headers=HEADERS)

    @app.get(PREFIX + '/app.js')
    async def script():
        return FileResponse(STATIC_ROOT / 'account-login.js', headers=HEADERS, media_type='text/javascript')

    @app.get(PREFIX + '/viewer')
    async def viewer_page(request: Request):
        viewer(request)
        return FileResponse(STATIC_ROOT / 'account-viewer.html', headers=HEADERS)

    @app.get(PREFIX + '/credentials')
    async def credentials(request: Request):
        lease = viewer(request)
        return response({'password': lease['desktop'].password})

    @app.get(PREFIX + '/novnc/{asset:path}')
    async def assets(asset: str, request: Request):
        viewer(request)
        return FileResponse(asset_path(asset), headers=HEADERS)

    connections = set()

    @app.websocket(PREFIX + '/websockify')
    async def gateway(websocket: WebSocket):
        ticket = websocket.cookies.get(COOKIE)
        lease = manager.viewer(ticket)
        if not same_origin(websocket, required=True) or not lease or len(connections) >= 2:
            await websocket.close(code=1008)
            return
        connections.add(websocket)
        tasks, writer = [], None
        try:
            await websocket.accept(subprotocol='binary' if 'binary' in websocket.scope.get('subprotocols', []) else None)
            # Endpoint is resolved only from the current lease; never client-controlled.
            reader, writer = await asyncio.wait_for(asyncio.open_connection('127.0.0.1', lease['desktop'].port), 3)

            async def incoming():
                while manager.viewer(ticket) is lease:
                    message = await websocket.receive_bytes()
                    if len(message) > 1024 * 1024 or manager.viewer(ticket) is not lease:
                        return
                    writer.write(message)
                    await writer.drain()

            async def outgoing():
                while manager.viewer(ticket) is lease:
                    data = await reader.read(65536)
                    if not data or manager.viewer(ticket) is not lease:
                        return
                    await websocket.send_bytes(data)

            async def authorization():
                while manager.viewer(ticket) is lease:
                    await asyncio.sleep(.5)

            tasks = [asyncio.create_task(fn()) for fn in (incoming, outgoing, authorization)]
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        except (WebSocketDisconnect, OSError, asyncio.TimeoutError, RuntimeError, KeyError):
            pass
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if writer:
                writer.close()
                try:
                    await asyncio.wait_for(writer.wait_closed(), 2)
                except (OSError, asyncio.TimeoutError):
                    pass
            connections.discard(websocket)
            try:
                await websocket.close()
            except RuntimeError:
                pass
