import asyncio
from contextlib import asynccontextmanager
import hmac
import hashlib
import json
import logging
import os
from pathlib import Path
import secrets

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from query_diagnostics import format_failure
from memory_usage import snapshot, log_memory
from engine import Engine
from scanner import Scanner, ScanError
from storage import Store
from telegram_api import Telegram

load_dotenv(Path(__file__).with_name('.env'))
_browser_dir = Path(__file__).with_name('.playwright-browsers')
if _browser_dir.exists():
    os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', str(_browser_dir))
# Never emit httpx request URLs: Telegram embeds its Token in the URL.
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('httpcore').setLevel(logging.WARNING)
security = HTTPBasic(auto_error=False)


@asynccontextmanager
async def lifespan(app):
    directory = os.getenv('DATA_DIR', './data')
    store = Store(directory)
    password = os.getenv('ADMIN_PASSWORD', '')
    if password and len(password) < 16:
        store.close()
        raise ValueError('ADMIN_PASSWORD 至少需要 16 个字符。')
    url = os.getenv('PUBLIC_URL') or os.getenv('RENDER_EXTERNAL_URL', '')
    app.state.engine = Engine(store, Scanner(directory), Telegram(os.getenv('TELEGRAM_BOT_TOKEN', '')),
                              os.getenv('TELEGRAM_BOT_USERNAME', 'activeSG_monitor_bot'), url,
                              os.getenv('TELEGRAM_MODE', 'webhook'), os.getenv('BINDING_CODE', ''))
    app.state.password = password
    app.state.csrf = secrets.token_urlsafe(32)
    log_memory('startup')
    app.state.engine.start()
    maintenance = asyncio.create_task(app.state.engine.scanner.maintenance())
    try:
        yield
    finally:
        maintenance.cancel()
        await asyncio.gather(maintenance, return_exceptions=True)
        await app.state.engine.close()


app = FastAPI(title='ActiveSG 空场提醒', lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


def admin(request: Request, credentials: HTTPBasicCredentials | None = Depends(security)):
    expected = request.app.state.password
    if not expected:
        raise HTTPException(503, '请先在 Render 设置 ADMIN_PASSWORD，设置后可访问管理页面。')
    if not credentials or not hmac.compare_digest(credentials.username.encode(), b'admin') or not hmac.compare_digest(credentials.password.encode(), expected.encode()):
        raise HTTPException(401, '请输入管理账号和密码。', headers={'WWW-Authenticate': 'Basic'})
    return True


@app.get('/healthz')
async def health(request: Request):
    engine = request.app.state.engine
    return {'status': 'ok', 'telegram_connected': engine.ready, 'monitor_configured': engine.config() is not None,
            'revision': os.getenv('RENDER_GIT_COMMIT'), 'features': ['telegram_human_browser']}


@app.post('/telegram/webhook')
async def webhook(request: Request):
    engine = request.app.state.engine
    expected = engine.store.get('webhook_secret')
    supplied = request.headers.get('x-telegram-bot-api-secret-token', '')
    if engine.mode != 'webhook' or not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(403, '请求来源无效。')
    length = request.headers.get('content-length')
    if length and (not length.isdecimal() or int(length) > 65536):
        raise HTTPException(413, '请求过长。')
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 65536:
            raise HTTPException(413, '请求过长。')
    try:
        update = json.loads(body)
        if not isinstance(update, dict):
            raise ValueError()
        engine.store.accept_update(update)
    except (ValueError, TypeError, UnicodeDecodeError):
        raise HTTPException(400, '消息格式无效。') from None
    # Durable queue: acknowledge immediately; slow browser work never blocks Telegram.
    return {'ok': True}


@app.get('/', response_class=HTMLResponse, dependencies=[Depends(admin)])
async def dashboard(request: Request):
    path = Path(__file__).with_name('dashboard.html')
    return HTMLResponse(path.read_text(encoding='utf-8').replace('__CSRF__', request.app.state.csrf),
                        headers={'Cache-Control': 'no-store', 'X-Frame-Options': 'DENY',
                                 'Referrer-Policy': 'no-referrer',
                                 'Content-Security-Policy': "default-src 'self'; script-src 'nonce-" + request.app.state.csrf + "'; style-src 'unsafe-inline'; img-src 'self' blob:; frame-ancestors 'none'"})


@app.get('/status', dependencies=[Depends(admin)])
async def status(request: Request):
    engine = request.app.state.engine
    return {'text': engine.status(), 'telegram_connected': engine.ready,
            'connection_error': engine.connection_error, 'bound': bool(engine.store.get('owner')),
            'browser_busy': engine.scanner.busy,
            'browser_active': engine.scanner.browser_active, 'human_waiting': bool(engine.scanner.handoff), 'memory': snapshot(),
            'binding_command': '/start ' + engine.binding_code if not engine.store.get('owner') else None,
            'persistent_disk_configured': os.getenv('DATA_DIR', '').startswith('/var/data')}


@app.get('/browser/screenshot', dependencies=[Depends(admin)])
async def screenshot(request: Request):
    try:
        image = await request.app.state.engine.scanner.screenshot()
        return Response(image, media_type='image/jpeg', headers={'Cache-Control': 'no-store'})
    except ScanError as error:
        raise HTTPException(409, str(error)) from None
    except Exception:
        raise HTTPException(503, '浏览器暂不可用，请运行 README 中的浏览器诊断。') from None


@app.post('/browser/action', dependencies=[Depends(admin)])
async def browser_action(request: Request):
    if not hmac.compare_digest(request.headers.get('x-monitor-csrf', ''), request.app.state.csrf):
        raise HTTPException(403, '请刷新管理页面后重试。')
    if len(await request.body()) > 4096:
        raise HTTPException(413, '输入过长。')
    try:
        data = await request.json()
        await request.app.state.engine.scanner.action(data.get('action'), data)
        return {'ok': True}
    except (ScanError, ValueError, KeyError, TypeError) as error:
        raise HTTPException(409, str(error)) from None
    except Exception:
        raise HTTPException(503, '浏览器操作未完成，请刷新画面。') from None


@app.get('/browser/diagnostic', dependencies=[Depends(admin)])
async def diagnostic(request: Request):
    value = request.app.state.engine.scanner.last_failure()
    return JSONResponse({'diagnostic': value, 'text': format_failure(value, include_url=True)},
                        headers={'Cache-Control': 'no-store'})


@app.get('/browser/diagnostic/screenshot', dependencies=[Depends(admin)])
async def diagnostic_screenshot(request: Request):
    scanner = request.app.state.engine.scanner
    value = scanner.last_failure()
    if not value or not value.get('screenshot_sha256'):
        raise HTTPException(404, '本次失败未保存截图，请查看文字诊断。')
    try:
        path = scanner.directory / 'last-query-failure.jpg'
        if path.stat().st_size > 750000:
            raise ValueError()
        image = path.read_bytes()
        if hashlib.sha256(image).hexdigest() != value['screenshot_sha256']:
            raise ValueError()
    except (OSError, ValueError):
        raise HTTPException(404, '截图暂不可用，请查看文字诊断。') from None
    return Response(image, media_type='image/jpeg', headers={'Cache-Control': 'no-store'})
