"""One-off PerfectGym login observation; no monitoring or reservation actions."""
import asyncio
from datetime import timedelta
import math
import os
from pathlib import Path
import re
import secrets
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from models import now_sg
from remote_browser import RemoteProfile, RemoteBrowserError, live_url, revoke


HOST = 'thekallang.perfectgym.com'
LOGIN_URL = 'https://' + HOST + '/clientportal2/#/Login'
BOOKING_LINK = re.compile(r'^\s*(?:book\s+a\s+(?:facility|court)|facility\s+bookings?|court\s+bookings?|book\s+(?:a\s+)?facility\s*/\s*court)\s*$', re.I)
LOGIN_TEXT = re.compile(r'^\s*log\s*in\s*$', re.I)

# Read only booking-related public labels. Never return input values, full page
# text, HTML, cookies, account widgets, or a screenshot of the login session.
READ_PORTAL = r"""() => {
  const visible = n => !!(n.getClientRects().length &&
    getComputedStyle(n).visibility !== 'hidden' &&
    !n.closest('[hidden],[aria-hidden="true"]'));
  const text = n => (n.innerText || n.textContent || '').replace(/\s+/g, ' ').trim();
  const privateText = /@|\b(?:welcome|hello|profile|account|member(?:ship)?|payment|billing|invoice|credit|password|e-?mail|phone|address|remember\s+me)\b/i;
  const safe = value => value && value.length <= 120 && !privateText.test(value);
  const booking = /\b(?:book(?:ing)?|reservations?|availability|calendar|schedule|select\s+(?:a\s+)?(?:facility|court|venue|date))\b/i;
  const venue = /\b(?:tennis|badminton|squash|courts?|facilit(?:y|ies)|venues?|sport(?:s)?\s+hall|stadium|swimming|pitch(?:es)?)\b/i;
  const dateText = /^(?:(?:mon|tue|wed|thu|fri|sat|sun)(?:day|sday|nesday|rsday|urday)?[,.]?\s*)?(?:\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[-/]\d{1,2}[-/]\d{2,4}|\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*(?:\s+\d{4})?|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2}(?:,?\s+\d{4})?)$/i;
  const unique = (items, count=20) => [...new Set(items.filter(safe))].slice(0, count);
  const headings = unique([...document.querySelectorAll('h1,h2,h3,h4,[role="heading"]')]
    .filter(visible).map(text).filter(s => booking.test(s) || venue.test(s)));
  const controls = [...document.querySelectorAll('a,button,[role="button"],option,legend,label,h1,h2,h3,h4,[role="heading"],time')]
    .filter(n => visible(n) && !n.closest('form:has(input[type="password"])'));
  const labels = controls.map(text);
  const venues = unique(labels.filter(s => venue.test(s) &&
    !/\b(?:book|booking|bookings|reservation|reservations|select|choose|availability|calendar|schedule)\b/i.test(s) &&
    !/^(?:courts?|facilit(?:y|ies)|venues?|tennis|badminton|squash)$/i.test(s)));
  const dates = unique(controls.flatMap(n => {
    const values = [text(n), n.getAttribute('aria-label') || ''];
    if(n.tagName === 'TIME') values.push(n.getAttribute('datetime') || '');
    return values.filter(s => dateText.test(s));
  }), 35);
  const filters = unique([...document.querySelectorAll('label,legend,select,[role="combobox"],button,[role="button"]')]
    .filter(visible).map(n => n.getAttribute('aria-label') || text(n))
    .filter(s => /^(?:select\s+|choose\s+)?(?:facility|court|venue|activity|sport|location|club|date|time|from|to)(?:\s*(?:type|:|\*|\?|selection))?$/i.test(s)), 12);
  const loginForm = !![...document.querySelectorAll('input[name="Password"]')].find(visible);
  const signOut = [...document.querySelectorAll('a,button,[role="button"]')]
    .some(n => visible(n) && [text(n), n.getAttribute('aria-label') || '', n.getAttribute('title') || '']
      .some(s => /^(?:log\s*out|sign\s*out)$/i.test(s.trim())));
  const rejected = [...document.querySelectorAll('[role="alert"],.validation-summary-errors,.field-validation-error')]
    .some(n => visible(n) && /(?:invalid|incorrect|wrong|failed|not\s+(?:found|valid)|not\s+recognised|could\s+not)/i.test(text(n)));
  const challenge = !!document.querySelector('iframe[src*="challenges.cloudflare.com"],iframe[src*="recaptcha"],iframe[src*="hcaptcha"],.cf-turnstile,[data-sitekey]');
  return {headings, venues, dates, filters, login_form: loginForm,
    signed_in: signOut, login_rejected: rejected, challenge,
    booking_area: headings.some(s => booking.test(s)) || (venues.length > 0 && filters.length > 0)};
}"""


class PortalFailure(Exception):
    """A fixed user-facing error, never a browser/provider exception."""


class KallangPortal:
    def __init__(self, directory):
        self.directory = Path(directory) / 'kallang'
        self.profile = RemoteProfile(self.directory)
        self.lock = asyncio.Lock()
        self.driver = self.browser = self.context = self.page = None
        self.handoff = None
        self.deadline = 0.0
        self._task = None
        self._live_cdp = None
        self._live_identifier = None
        self._username = ''
        self._submitted = False

    @property
    def busy(self):
        return self.lock.locked()

    def valid_handoff(self, identifier):
        return bool(self.handoff and self.handoff['id'] == identifier and
                    self.handoff['deadline'] > time.monotonic() and self.busy)

    def _remaining(self):
        return max(0, self.deadline - time.monotonic())

    def _check_host(self):
        parsed = urlsplit(self.page.url)
        if parsed.scheme != 'https' or parsed.hostname != HOST or parsed.username or parsed.password:
            raise PortalFailure('新站跳转到了尚未确认的地址，已停止操作；不会在那里输入账号密码。')

    def _result(self, status, message, observed=None):
        result = {'status': status, 'message': message,
                  'headings': [], 'venues': [], 'dates': [], 'filters': []}
        observed = observed or {}
        for key in ('headings', 'venues', 'dates', 'filters'):
            values = []
            for value in observed.get(key, [])[:35]:
                if not isinstance(value, str) or len(value) > 120 or '@' in value:
                    continue
                if self._username and self._username.casefold() in value.casefold():
                    continue
                account = self._username.split('@', 1)[0]
                if len(account) >= 4 and account.casefold() in value.casefold():
                    continue
                values.append(value)
            result[key] = list(dict.fromkeys(values))
        if status == 'ready':
            confirmed = dict(observed, **{key: result[key] for key in ('headings', 'venues', 'dates', 'filters')})
            if not self._ready(confirmed):
                result['status'] = 'signed_in' if observed.get('signed_in') else 'needs_human'
                result['message'] = '尚未同时确认具体场地、日期及登录后的预约页面，需要人工确认；未设置新站监控。'
        return result

    async def _snapshot(self):
        self._check_host()
        snapshot = await asyncio.wait_for(self.page.evaluate(READ_PORTAL), timeout=min(6, max(0.1, self._remaining())))
        self._check_host()
        filtered = self._result('observed', '', snapshot)
        for key in ('headings', 'venues', 'dates', 'filters'):
            snapshot[key] = filtered[key]
        return snapshot

    def _ready(self, snapshot):
        return bool((snapshot.get('signed_in') or self._submitted) and
                    not snapshot.get('login_form') and snapshot.get('booking_area') and
                    snapshot.get('venues') and snapshot.get('dates'))

    async def _start(self):
        from playwright.async_api import async_playwright
        if not os.getenv('BROWSERLESS_TOKEN', '').strip():
            raise PortalFailure('请先在 Render 设置 BROWSERLESS_TOKEN，再尝试新站登录。')
        endpoint = await self.profile.endpoint()
        seconds = min(120, self.profile.timeout_seconds)
        parts = urlsplit(endpoint)
        query = dict(parse_qsl(parts.query))
        query['timeout'] = str(seconds * 1000)
        endpoint = urlunsplit(parts._replace(query=urlencode(query)))
        self.deadline = time.monotonic() + seconds - 3
        self.driver = await async_playwright().start()
        try:
            self.browser = await self.driver.chromium.connect_over_cdp(endpoint, timeout=20000)
        except Exception:
            try:
                self.profile.forget()
            except OSError:
                pass
            raise PortalFailure('新站远程浏览器连接未完成，请稍后重试；不会占用 ActiveSG 的登录档案。') from None
        if not self.browser.contexts:
            raise PortalFailure('新站远程浏览器未提供可用会话，请稍后重试。')
        self.context = self.browser.contexts[0]
        self.page = next((p for p in self.context.pages if urlsplit(p.url).hostname == HOST), None)
        if self.page is None:
            self.page = await self.context.new_page()

        async def restrict_navigation(route):
            request = route.request
            if request.is_navigation_request() and request.frame == self.page.main_frame:
                target = urlsplit(request.url)
                if target.scheme != 'https' or target.hostname != HOST or target.username or target.password:
                    await route.abort()
                    return
            await route.continue_()

        await self.page.route('**/*', restrict_navigation)
        self.page.set_default_timeout(10000)
        await self.page.set_viewport_size({'width': 1280, 'height': 900})
        await self.page.goto(LOGIN_URL, wait_until='domcontentloaded', timeout=25000)
        self._check_host()

    async def _login(self):
        self._username = os.getenv('KALLANG_LOGIN_USERNAME', '').strip()
        password = os.getenv('KALLANG_LOGIN_PASSWORD', '')
        if not self._username or not password:
            raise PortalFailure('请在 Render 设置 KALLANG_LOGIN_USERNAME 和 KALLANG_LOGIN_PASSWORD；不需要发送到聊天。')
        self._check_host()
        login = self.page.locator('input[name="Login"]:visible')
        secret = self.page.locator('input[name="Password"]:visible')
        if await login.count() != 1 or await secret.count() != 1:
            raise PortalFailure('新站登录表单与已确认的页面不一致，已停止自动输入。')
        await login.fill(self._username)
        self._check_host()
        await secret.fill(password)
        password = None
        # Do not select Remember me. No submit-by-key fallback: choose only a
        # visible Login control so registration/reset links are never activated.
        candidates = self.page.get_by_role('button', name=LOGIN_TEXT)
        visible = [candidates.nth(i) for i in range(await candidates.count())
                   if await candidates.nth(i).is_visible()]
        if not visible:
            candidates = self.page.get_by_text(LOGIN_TEXT)
            visible = [candidates.nth(i) for i in range(await candidates.count())
                       if await candidates.nth(i).is_visible() and
                       await candidates.nth(i).evaluate('(n) => !/^H[1-6]$/.test(n.tagName)')]
        if len(visible) != 1:
            raise PortalFailure('无法唯一确认 Login 登录控件，已停止操作。')
        self._check_host()
        await visible[0].click(timeout=10000, no_wait_after=True)
        self._submitted = True

    async def _booking_entry(self):
        self._check_host()
        candidates = self.page.get_by_text(BOOKING_LINK)
        visible = [candidates.nth(i) for i in range(await candidates.count())
                   if await candidates.nth(i).is_visible() and
                   await candidates.nth(i).evaluate('(n) => !/^H[1-6]$/.test(n.tagName)')]
        if len(visible) == 1:
            await visible[0].click(timeout=min(10000, int(max(1, self._remaining() - 8) * 1000)), no_wait_after=True)
            return True
        return False

    async def _human(self, callback, observed):
        if callback is None or self._remaining() < 15:
            return self._result('signed_in' if observed.get('signed_in') else 'needs_human',
                                '尚未同时看到具体场地和日期，需要人工进入场地预约页面后确认。', observed)
        self._live_cdp = await self.context.new_cdp_session(self.page)
        completion = {'reason': None}

        def completed(event):
            if event.get('liveURLId') == self._live_identifier:
                completion['reason'] = event.get('reason')

        self._live_cdp.on('Browserless.liveComplete', completed)
        response = await asyncio.wait_for(self._live_cdp.send('Browserless.liveURL', {
            'quality': 30, 'type': 'jpeg', 'interactable': True, 'resizable': False,
            'showBrowserInterface': False, 'timeout': int(min(90, self._remaining() - 8) * 1000),
            'instructions': '请完成 The Kallang 登录，进入场地预约页面并打开具体场地的日期列表。'
                            '请勿提交预约或付款。看到场地和日期后点击 Done。'}), timeout=8)
        self._live_identifier = response.get('liveURLId')
        if response.get('error') or not self._live_identifier:
            raise PortalFailure('新站人工浏览器入口未能创建，请稍后重试。')
        url = live_url(response['liveURL'], os.getenv('BROWSERLESS_TOKEN', '').strip())
        seconds = min(float(response['timeout']) / 1000, 90, self._remaining() - 5)
        if not math.isfinite(seconds) or seconds <= 0:
            raise PortalFailure('新站人工入口已过期，请重新开始登录检查。')
        self.handoff = {'id': secrets.token_hex(16), 'url': url,
                        'deadline': time.monotonic() + seconds,
                        'expires_at': (now_sg() + timedelta(seconds=seconds)).strftime('%Y-%m-%d %H:%M:%S')}
        await callback(dict(self.handoff))
        done_deadline = None
        while self.valid_handoff(self.handoff['id']) and self._remaining() > 5:
            try:
                observed = await self._snapshot()
            except PortalFailure:
                raise
            except Exception:
                await asyncio.sleep(0.75)
                continue  # A user navigation may replace the execution context.
            if self._ready(observed):
                return self._result('ready', '已进入新站场地预约页面，并读取到场地及日期；尚未设置新站监控。', observed)
            if completion['reason'] in ('userFailed', 'closed', 'timeout'):
                break
            if completion['reason'] == 'userDone':
                if done_deadline is None:
                    done_deadline = time.monotonic() + 3
                elif time.monotonic() >= done_deadline:
                    break
            await asyncio.sleep(1)
        return self._result('signed_in' if observed.get('signed_in') else 'needs_human',
                            '本次尚未同时确认具体场地和日期；人工入口已结束，未设置新站监控。', observed)

    async def inspect(self, on_human=None):
        if self.busy:
            return self._result('busy', '新站登录检查正在进行，请等待本次操作结束。')
        async with self.lock:
            self._task = asyncio.current_task()
            self._submitted = False
            # Also redact account identifiers when a saved remote profile is
            # already signed in and no automatic form submission is needed.
            self._username = os.getenv('KALLANG_LOGIN_USERNAME', '').strip()
            observed = {}
            try:
                await self._start()
                # The SPA can initially render no controls; allow its confirmed
                # login form or a real booking screen to appear.
                until = min(time.monotonic() + 10, self.deadline - 15)
                while True:
                    observed = await self._snapshot()
                    if observed.get('login_form') or observed.get('signed_in') or self._ready(observed) or time.monotonic() >= until:
                        break
                    await asyncio.sleep(0.5)
                if self._ready(observed):
                    return self._result('ready', '已进入新站场地预约页面，并读取到场地及日期；尚未设置新站监控。', observed)
                if observed.get('login_form') and not observed.get('challenge'):
                    await self._login()
                    until = min(time.monotonic() + 12, self.deadline - 15)
                    while time.monotonic() < until:
                        await asyncio.sleep(0.5)
                        observed = await self._snapshot()
                        if observed.get('login_form') and observed.get('login_rejected'):
                            return self._result('login_failed', '新站未接受本次登录，请在 Render 核对账号密码；不要把密码发送到聊天。')
                        if not observed.get('login_form') or observed.get('challenge'):
                            break
                if self._ready(observed):
                    return self._result('ready', '已进入新站场地预约页面，并读取到场地及日期；尚未设置新站监控。', observed)
                if not observed.get('login_form') and self._remaining() > 20:
                    if await self._booking_entry():
                        until = min(time.monotonic() + 8, self.deadline - 15)
                        while time.monotonic() < until:
                            await asyncio.sleep(0.5)
                            observed = await self._snapshot()
                            if self._ready(observed):
                                return self._result('ready', '已进入新站场地预约页面，并读取到场地及日期；尚未设置新站监控。', observed)
                return await self._human(on_human, observed)
            except asyncio.CancelledError:
                raise
            except PortalFailure as error:
                return self._result('error', str(error))
            except RemoteBrowserError as error:
                # RemoteProfile errors are fixed, sanitized provider messages.
                return self._result('error', 'The Kallang 浏览器未启动：' + str(error))
            except Exception:
                return self._result('error', '新站登录检查未完成，可能是浏览器连接、页面加载或人工入口超时；未记录账号密码，也未设置新站监控。')
            finally:
                await self._cleanup()
                self._task = None

    async def _cleanup(self):
        self.handoff = None
        if self._live_cdp is not None:
            if self._live_identifier:
                await revoke(self._live_cdp, self._live_identifier)
            try:
                await asyncio.wait_for(self._live_cdp.detach(), timeout=3)
            except Exception:
                pass
        self._live_cdp = self._live_identifier = None
        browser, driver = self.browser, self.driver
        self.browser = self.context = self.page = self.driver = None
        self._username = ''
        self._submitted = False
        if browser is not None:
            try:
                await asyncio.wait_for(browser.close(), timeout=8)
            except Exception:
                pass
        if driver is not None:
            try:
                await asyncio.wait_for(driver.stop(), timeout=8)
            except Exception:
                pass

    async def close(self):
        task = self._task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        async with self.lock:
            await self._cleanup()
