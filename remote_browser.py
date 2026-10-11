"""Browserless profiles and short-lived human access. Credentials stay server-side."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit, unquote

import httpx


class RemoteBrowserError(Exception):
    pass


def provider():
    value = os.getenv('BROWSER_PROVIDER', 'auto').strip().lower()
    if value == 'auto':
        return 'browserless' if os.getenv('BROWSERLESS_TOKEN', '').strip() else 'local'
    if value not in ('local', 'browserless'):
        raise RemoteBrowserError('BROWSER_PROVIDER 应为 auto、local 或 browserless。')
    return value


def vendor_url(value, scheme):
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (ValueError, TypeError):
        raise RemoteBrowserError('远程浏览器连接地址无法确认。') from None
    host = parsed.hostname or ''
    if (parsed.scheme != scheme or not host.endswith('.browserless.io')
            or parsed.username or parsed.password or port not in (None, 443)):
        raise RemoteBrowserError('远程浏览器返回了不受信任的连接地址。')
    return value


def live_url(value, token):
    vendor_url(value, 'https')
    # A viewer link is a temporary capability, never the server API credential.
    if token and (token in value or token in unquote(value)):
        raise RemoteBrowserError('远程入口包含服务凭据，未发送。')
    return value


class RemoteProfile:
    def __init__(self, directory):
        self.path = Path(directory) / 'browserless-profile.json'
        self.timeout_seconds = 120

    async def endpoint(self):
        token = os.getenv('BROWSERLESS_TOKEN', '').strip()
        if not token:
            raise RemoteBrowserError('请在 Render 设置 BROWSERLESS_TOKEN；不会改用本机浏览器。')
        region = os.getenv('BROWSERLESS_REGION', 'sfo').strip()
        if region not in ('sfo', 'lon', 'ams'):
            raise RemoteBrowserError('BROWSERLESS_REGION 应为 sfo、lon 或 ams。')
        try:
            self.timeout_seconds = int(os.getenv('BROWSERLESS_TIMEOUT_SECONDS', '120'))
            if not 60 <= self.timeout_seconds <= 1800:
                raise ValueError()
        except ValueError:
            raise RemoteBrowserError('BROWSERLESS_TIMEOUT_SECONDS 应为 60–1800；免费方案请保持 120。') from None
        signature = hashlib.sha256((region + ':' + token).encode()).hexdigest()
        saved = None
        try:
            saved = json.loads(self.path.read_text(encoding='utf-8'))
            if (saved.get('credential_hash') != signature or saved.get('expires', 0) <= time.time() + 180
                    or saved.get('region') != region):
                saved = None
        except (OSError, ValueError, AttributeError, TypeError):
            saved = None
        if saved is None:
            try:
                async with httpx.AsyncClient(timeout=20) as client:
                    response = await client.post(f'https://production-{region}.browserless.io/session',
                                                 params={'token': token},
                                                 json={'ttl': 86400000, 'browser': 'chrome', 'headless': False,
                                                       'stealth': False, 'processKeepAlive': 0, 'replay': False})
                if not response.is_success:
                    descriptions = {401: 'Token 无效', 403: 'Token 或账号权限不可用',
                                    429: '额度不足或已有会话占用'}
                    raise RemoteBrowserError('Browserless 会话创建失败：' + descriptions.get(
                        response.status_code, f'HTTP {response.status_code}') + '；请查看 Browserless 账号。')
                data = response.json()
                connect = vendor_url(data['connect'], 'wss')
                ttl = data.get('ttl', 86400000)
                if not isinstance(ttl, (int, float)) or not 0 < ttl <= 86400000:
                    raise RemoteBrowserError('Browserless 会话有效期无法确认。')
                saved = {'connect': connect, 'expires': time.time() + ttl / 1000,
                         'credential_hash': signature, 'region': region}
                self.path.parent.mkdir(parents=True, exist_ok=True)
                temporary = self.path.with_suffix('.tmp')
                with open(temporary, 'w', encoding='utf-8') as handle:
                    os.chmod(temporary, 0o600)
                    json.dump(saved, handle)
                temporary.replace(self.path)
            except RemoteBrowserError:
                raise
            except (httpx.HTTPError, OSError, ValueError, KeyError, TypeError):
                raise RemoteBrowserError('Browserless 网络连接或会话保存失败；未使用本机浏览器。') from None
        parsed = urlsplit(vendor_url(saved['connect'], 'wss'))
        query = dict(parse_qsl(parsed.query))
        query['timeout'] = str(self.timeout_seconds * 1000)
        # Never enable solver, proxies or stealth. This route is manual intervention.
        return urlunsplit(parsed._replace(query=urlencode(query)))

    def forget(self):
        # Let the vendor's TTL remove an expired/deleted profile; do not delete a
        # working remote profile at every disconnect or service deployment.
        self.path.unlink(missing_ok=True)


async def revoke(cdp, identifier):
    try:
        await asyncio.wait_for(cdp.send('Browserless.closeLiveURL', {'liveURLId': identifier}), timeout=3)
    except Exception:
        pass  # Browser shutdown/expiry also revokes the viewer.
