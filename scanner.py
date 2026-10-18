import asyncio
import hashlib
import json
from datetime import date, datetime
import os
import logging
import math
import time
from pathlib import Path
import re
import unicodedata
from urllib.parse import parse_qs, urljoin, urlparse

from query_diagnostics import classify, safe_url, load_failure
from remote_browser import RemoteProfile, RemoteBrowserError, provider, live_url, revoke
from memory_usage import log_memory
from models import now_sg, slot_from_control, SG
from slot_reader import READ_SLOTS
from page_scrolling import SCROLL_LIST, TARGET_POSITION

BASE = 'https://activesg.gov.sg'
ACTIVITIES = BASE + '/facility-bookings/activities'


class ScanError(Exception):
    pass


class VerificationRequired(ScanError):
    def __init__(self, message, diagnostic=None):
        super().__init__(message)
        self.diagnostic = diagnostic


class SecurityCheckPending(VerificationRequired):
    pass


def official_url(value, origin=BASE):
    url = urljoin(origin, value)
    parsed = urlparse(url)
    if parsed.scheme != 'https' or parsed.hostname != 'activesg.gov.sg' or not parsed.path.startswith('/facility-bookings/'):
        raise ScanError('官网链接结构发生变化，本次查询不采用。')
    return url


def assert_date(requested, current_url, buttons):
    actual = parse_qs(urlparse(current_url).query).get('date', [''])[0]
    if actual and actual != requested:
        return False
    selected = [b for b in buttons if b.get('selected')]
    expected = date.fromisoformat(requested).strftime('%a, %d %b').replace(', 0', ', ')
    for b in selected:
        label = b.get('label', '').replace(', 0', ', ')
        if expected in label:
            return True
    if selected:
        return False
    raise ScanError('无法确认官网选中的日期，本次查询不采用。')


class Scanner:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.lock = asyncio.Lock()
        self.playwright = self.browser = self.context = self.page = None
        self.busy = False
        self.progress = {}
        self.last_manual_use = 0.0
        self.manual_idle_seconds = 120
        self.document_response = {}
        self.remote_profile = RemoteProfile(directory)
        self.remote_deadline = 0.0
        self.handoff = None
        self.on_human = None
        self.is_current = lambda: True

    def mark_step(self, stage, target='', day=None):
        self.progress = {'stage': stage, 'target': target[:200], 'date': day,
                         'started': time.monotonic(), 'operation': stage}

    def mark_operation(self, operation, timeout=None, **details):
        if not self.progress:
            self.mark_step('页面操作')
        self.progress.update(operation=operation, timeout_seconds=timeout, **details)

    async def progress_evidence(self, diagnostic):
        if not self.progress:
            return None
        step = {key: value for key, value in self.progress.items() if key != 'started'}
        step['elapsed_seconds'] = round(max(0, time.monotonic() - self.progress['started']), 1)
        if self.remote_deadline:
            step['browser_seconds_remaining'] = round(max(0, self.remote_deadline - time.monotonic()), 1)
        # Only small public-page UI observations. Never save cookies, fields,
        # complete Playwright errors (which can contain connection credentials),
        # or private viewer URLs.
        if self.page is not None and diagnostic.get('category') == 'page_loaded':
            parsed = urlparse(self.page.url)
            if parsed.hostname == 'activesg.gov.sg' and parsed.path.startswith('/facility-bookings/'):
                try:
                    step['page'] = await asyncio.wait_for(self.page.evaluate(r"""() => ({
                      date_buttons: document.querySelectorAll('[aria-label^="View timeslots for"]').length,
                      slot_controls: document.querySelectorAll('input[type="checkbox"]').length,
                      dialogs: [...document.querySelectorAll('dialog,[role="dialog"],[aria-modal="true"]')]
                        .filter(n => n.getClientRects().length).length,
                      headings: [...document.querySelectorAll('h1,h2,h3,[role="heading"]')]
                        .filter(n => n.getClientRects().length)
                        .map(n => (n.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 80)).slice(0, 6)
                    })"""), timeout=2)
                except Exception:
                    pass
        return step

    @property
    def browser_active(self):
        return self.context is not None or self.browser is not None

    async def release_browser(self):
        # Caller holds the browser lock. CDP only disconnects; its borrowed
        # context and pages belong to the external browser and are never closed.
        browser, context, driver = self.browser, self.context, self.playwright
        self.browser = self.context = self.page = self.playwright = None
        self.last_manual_use = 0.0
        try:
            if browser is not None:
                await asyncio.wait_for(browser.close(), timeout=10)
            elif context is not None:
                await asyncio.wait_for(context.close(), timeout=10)
        except Exception:
            logging.getLogger('uvicorn.error').warning('Browser cleanup did not complete; stopping its driver.')
        finally:
            if driver is not None:
                try:
                    await asyncio.wait_for(driver.stop(), timeout=10)
                except Exception:
                    logging.getLogger('uvicorn.error').warning('Browser driver cleanup did not complete.')
            if browser is not None or context is not None or driver is not None:
                log_memory('browser_released')

    async def release_if_idle(self):
        # A query or interaction owns this lock; never interrupt it for idle cleanup.
        if self.lock.locked() or not self.browser_active or not self.last_manual_use:
            return
        async with self.lock:
            if self.last_manual_use and time.monotonic() - self.last_manual_use >= self.manual_idle_seconds:
                await self.release_browser()

    async def maintenance(self):
        next_log = 0.0
        while True:
            await self.release_if_idle()
            if time.monotonic() >= next_log:
                log_memory('periodic', self.browser_active)
                next_log = time.monotonic() + 30
            await asyncio.sleep(5)

    async def close_extra_page(self, page):
        # Local contexts only. Closing the context can race a popup close event.
        try:
            if page is not self.page:
                await page.close()
        except Exception:
            pass

    async def ensure_page(self):
        if self.page and not self.page.is_closed():
            return self.page
        # Dispose stale contexts before replacing them, including partial launches.
        await self.release_browser()
        from playwright.async_api import async_playwright
        log_memory('before_browser_start')
        try:
            self.playwright = await async_playwright().start()
            remote = provider() == 'browserless'
            endpoint = await self.remote_profile.endpoint() if remote else os.getenv('BROWSER_CDP_URL', '')
            self.remote_deadline = time.monotonic() + self.remote_profile.timeout_seconds - 3 if remote else 0.0
            if endpoint:
                try:
                    self.browser = await self.playwright.chromium.connect_over_cdp(endpoint, timeout=20000)
                except Exception:
                    if remote:
                        self.remote_profile.forget()
                        raise RemoteBrowserError('Browserless 连接失败；请查看账号额度、区域和会话限制。下轮会重新建立会话。') from None
                    raise
                if not self.browser.contexts:
                    raise ScanError('远程浏览器没有可用的会话。')
                self.context = self.browser.contexts[0]
                self.page = next((p for p in self.context.pages if urlparse(p.url).hostname == 'activesg.gov.sg'), None)
                if self.page is None:
                    self.page = await self.context.new_page()
            else:
                profile = self.directory / 'browser-profile'
                profile.mkdir(parents=True, exist_ok=True)
                self.context = await self.playwright.chromium.launch_persistent_context(
                    str(profile), headless=os.getenv('BROWSER_HEADLESS', 'true').lower() != 'false',
                    viewport={'width': 1280, 'height': 900}, locale='en-SG', timezone_id='Asia/Singapore',
                    args=['--disable-dev-shm-usage'])
                self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
                # A saved local profile may restore extra tabs. Keep one local tab.
                for extra in list(self.context.pages):
                    if extra is not self.page:
                        await extra.close()
                self.context.on('page', self.close_extra_page)
            if remote:
                await self.page.set_viewport_size({'width': 1280, 'height': 900})
            self.document_response = {}
            self.page.on('response', self.observe_response)
            self.page.set_default_timeout(15000)
            log_memory('after_browser_start', True)
        except BaseException as error:
            await self.release_browser()
            if isinstance(error, RemoteBrowserError):
                raise ScanError(str(error)) from None
            if isinstance(error, (ScanError, asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            raise ScanError('云端浏览器启动失败，请检查 Chromium 安装、系统依赖和服务内存；详情见浏览器诊断。') from None
        return self.page

    def observe_response(self, response):
        # Only the main document proves what page was served; ignore subresources.
        try:
            request = response.request
            if request.is_navigation_request() and request.frame == self.page.main_frame:
                self.document_response = {
                    'url': safe_url(response.url), 'status': response.status,
                    'headers': {k: v[:100] for k, v in response.headers.items()
                                if k.lower() in ('cf-mitigated', 'cf-ray', 'server')}}
        except Exception:
            pass

    async def inspect_page(self, page):
        title = await page.title()
        text = (await page.locator('body').inner_text())[:20000]
        markers = await page.evaluate('''() => ({
          challenge_options: typeof window._cf_chl_opt === 'object',
          challenge_script: [...document.querySelectorAll('script[src]')].some(
            n => n.src.includes('/cdn-cgi/challenge-platform/')),
          widget: [...document.querySelectorAll('iframe[src]')].some(
            n => {try {return new URL(n.src).hostname === 'challenges.cloudflare.com'} catch {return false}})
        })''')
        document = self.document_response if self.document_response.get('url') == safe_url(page.url) else {}
        return classify(page.url, title, text, document, markers), text

    async def guard(self, page):
        diagnostic, text = await self.inspect_page(page)
        category = diagnostic['category']
        if category in ('cloudflare_challenge', 'cloudflare_likely', 'cloudflare_widget', 'unknown_security'):
            raise SecurityCheckPending(diagnostic['label'] + '尚未完成。', diagnostic)
        if category == 'login':
            raise VerificationRequired('实际页面已跳转到 ActiveSG / Singpass 登录入口。', diagnostic)
        if category == 'access_denied':
            raise VerificationRequired('官网主页面拒绝访问；拦截来源尚未确认。', diagnostic)
        return text

    async def save_failure(self, error):
        # Persist a small metadata file before closing Chromium. No HTML, cookies,
        # form values or complete headers are recorded.
        diagnostic = getattr(error, 'diagnostic', None)
        if diagnostic is None and self.page is not None:
            try:
                diagnostic, _ = await asyncio.wait_for(self.inspect_page(self.page), timeout=3)
            except Exception:
                pass
        diagnostic = dict(diagnostic or {'category': 'unavailable', 'label': '未取得页面证据',
                                        'evidence': ['页面可能未启动、已关闭或无法读取']})
        step = await self.progress_evidence(diagnostic)
        if step:
            diagnostic['step'] = step
        diagnostic.update(captured_at=now_sg().isoformat(), error=str(error)[:300], screenshot_sha256=None)
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            # Never screenshot login/identity pages. Only the official facility
            # page in a failure state is captured, and only the current viewport.
            if self.page is not None and diagnostic['category'] in (
                    'cloudflare_challenge', 'cloudflare_likely', 'cloudflare_widget',
                    'unknown_security', 'access_denied', 'blank', 'page_loaded'):
                parsed = urlparse(self.page.url)
                if parsed.hostname == 'activesg.gov.sg' and parsed.path.startswith('/facility-bookings/'):
                    try:
                        image = await self.page.screenshot(type='jpeg', quality=40, timeout=5000)
                        if len(image) <= 750000:
                            temporary = self.directory / 'last-query-failure.jpg.tmp'
                            temporary.write_bytes(image)
                            temporary.replace(self.directory / 'last-query-failure.jpg')
                            diagnostic['screenshot_sha256'] = hashlib.sha256(image).hexdigest()
                    except Exception:
                        diagnostic['screenshot_note'] = '截图未完成，文字诊断仍已保存'
            temporary = self.directory / 'last-query-failure.json.tmp'
            temporary.write_text(json.dumps(diagnostic, ensure_ascii=False), encoding='utf-8')
            temporary.replace(self.directory / 'last-query-failure.json')
            logging.getLogger('uvicorn.error').info('query_diagnostic=%s', json.dumps(diagnostic, ensure_ascii=False))
        except Exception:
            logging.getLogger('uvicorn.error').warning('Failed to persist query diagnosis.')

    def last_failure(self):
        return load_failure(self.directory)

    def valid_handoff(self, identifier):
        item = self.handoff
        return bool(item and item['id'] == identifier and item['deadline'] > time.monotonic()
                    and self.is_current())

    def human_message(self):
        item = self.handoff
        if not item or not self.valid_handoff(item['id']):
            return None
        return ('ActiveSG 需要你手动处理网站验证。\n'
                '请立即打开以下远程浏览器入口（iPhone 可用 Safari 打开）：\n' + item['url'] +
                '\n入口于 ' + item['expires_at'] + '（新加坡时间）失效。\n'
                '在画面中完成验证，看到 ActiveSG 场地页面后等待自动查询。'
                '本次请不要发送 /pause，它会取消入口。\n'
                '入口失效后发送 /browser 重试。请勿转发该入口。')

    async def human_intervention(self, page, error, poll_interval=1):
        from playwright.async_api import Error as BrowserError
        if not self.on_human or not self.is_current():
            raise VerificationRequired('人工操作未开始或查询已取消。', error.diagnostic)
        remaining = self.remote_deadline - time.monotonic() - 20
        if remaining < 10:
            raise VerificationRequired('本次远程会话剩余时间不足，请发送 /browser 重新查询。', error.diagnostic)
        cdp = await self.context.new_cdp_session(page)
        state = {'reason': None, 'id': None}
        def completed(event):
            if event.get('liveURLId') == state['id']:
                state['reason'] = event.get('reason')
        cdp.on('Browserless.liveComplete', completed)
        identifier = None
        try:
            try:
                response = await asyncio.wait_for(cdp.send('Browserless.liveURL', {
                    'quality': 30, 'type': 'jpeg', 'interactable': True, 'resizable': False,
                    'showBrowserInterface': False, 'timeout': int(min(90, remaining) * 1000),
                    'instructions': '请在页面中手动完成网站验证。看到 ActiveSG 页面后等待程序继续查询。'
                                    '如无法完成，可选择 Couldn’t finish。'}), timeout=8)
                identifier = response.get('liveURLId')
                if response.get('error') or not identifier:
                    raise RemoteBrowserError('Browserless 无法生成操作入口，请检查账号的 LiveURL 支持和剩余会话时间。')
                state['id'] = identifier
                url = live_url(response['liveURL'], os.getenv('BROWSERLESS_TOKEN', '').strip())
                applied = float(response['timeout']) / 1000
                if not math.isfinite(applied) or applied <= 0:
                    raise ValueError()
                seconds = min(applied, remaining, 90)
            except RemoteBrowserError as failure:
                raise VerificationRequired(str(failure), error.diagnostic) from None
            except Exception:
                raise VerificationRequired('远程操作入口创建失败，请检查 Browserless 账号；未发送服务凭据。', error.diagnostic) from None
            from datetime import timedelta
            import secrets
            self.handoff = {'id': secrets.token_hex(16), 'url': url,
                            'deadline': time.monotonic() + seconds,
                            'expires_at': (now_sg() + timedelta(seconds=seconds)).strftime('%Y-%m-%d %H:%M:%S')}
            await self.on_human(self.handoff)
            while time.monotonic() < self.handoff['deadline']:
                if not self.is_current():
                    raise VerificationRequired('查询条件已更改或监控已暂停，人工入口已取消。', error.diagnostic)
                try:
                    text = await self.guard(page)
                    # Completion buttons are a viewer's report, not proof of clearance.
                    # A blank page or redirect also cannot confirm successful validation.
                    if (urlparse(page.url).hostname == 'activesg.gov.sg'
                            and urlparse(page.url).path.startswith('/facility-bookings/')
                            and len(text.strip()) >= 100):
                        return text
                except SecurityCheckPending:
                    pass
                except BrowserError as failure:
                    if 'Execution context was destroyed' not in str(failure):
                        raise VerificationRequired('远程浏览器会话已结束，尚未确认验证成功；请发送 /browser 重试。', error.diagnostic) from None
                if state['reason'] in ('userFailed', 'closed', 'timeout'):
                    raise VerificationRequired('人工处理未完成或入口已关闭，未读取本轮场地数据；请发送 /browser 重试。', error.diagnostic)
                if state['reason'] == 'userDone':
                    # The link has closed; allow automatic navigation to settle briefly.
                    for _ in range(5):
                        await asyncio.sleep(poll_interval)
                        if not self.is_current():
                            break
                        try:
                            text = await self.guard(page)
                            if (urlparse(page.url).hostname == 'activesg.gov.sg'
                                    and urlparse(page.url).path.startswith('/facility-bookings/') and len(text.strip()) >= 100):
                                return text
                        except SecurityCheckPending:
                            pass
                    raise VerificationRequired('点击完成后仍未确认进入场地页面，请发送 /browser 重试。', error.diagnostic)
                await asyncio.sleep(poll_interval)
            raise VerificationRequired('人工操作入口已过期，尚未确认验证成功。请发送 /browser 获取新入口。', error.diagnostic)
        finally:
            self.handoff = None
            if identifier:
                await revoke(cdp, identifier)
            try:
                await cdp.detach()
            except Exception:
                pass

    async def wait_until_ready(self, page, timeout=45, poll_interval=1):
        # A challenge page can finish its own checks and redirect automatically.
        # Waiting lets the site's scripts run; no checkbox or challenge is automated.
        from playwright.async_api import Error as BrowserError
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            try:
                return await self.guard(page)
            except SecurityCheckPending as error:
                if asyncio.get_running_loop().time() >= deadline:
                    if self.remote_deadline:
                        return await self.human_intervention(page, error, poll_interval)
                    label = (error.diagnostic or {}).get('label', '来源未确认的安全检查')
                    raise VerificationRequired(
                        f'{label}等待 {timeout:g} 秒后仍未完成，未读取到场地数据。'
                        '可发送 /diagnose 查看失败时保存的证据。', error.diagnostic) from None
            except BrowserError as error:
                # Automatic navigation can replace the execution context mid-read.
                if 'Execution context was destroyed' not in str(error) or asyncio.get_running_loop().time() >= deadline:
                    raise
            await asyncio.sleep(poll_interval)

    async def goto(self, page, url):
        # Start the document; find_link/select_day/read_slot_controls wait for
        # their actual content, independently of unrelated images and scripts.
        self.mark_operation('打开活动页', timeout=45)
        await page.goto(official_url(url), wait_until='commit', timeout=45000)
        self.mark_operation('读取活动页', timeout=15)
        return await self.wait_until_ready(page, timeout=5 if self.remote_deadline else 45)

    async def scroll_search(self, page, reset=False):
        if not self.is_current():
            raise ScanError('本轮查询已取消。')
        if not self.progress:
            self.mark_step('查找页面按钮')
        official_url(page.url)
        # Search may need to reveal cards that do not exist before scrolling.
        state = await page.evaluate(SCROLL_LIST, reset)
        if not reset:
            self.progress['search_scroll_steps'] = self.progress.get('search_scroll_steps', 0) + 1
            self.progress['search_scroll_distance'] = self.progress.get('search_scroll_distance', 0) + state['distance']
        self.progress['scrollable_panels'] = state['containers']
        self.progress['search_at_bottom'] = state['at_bottom']
        return state

    async def find_link(self, page, name, kind='activity', timeout=12, poll_interval=0.5):
        # Framework navigation can leave a document with headings but no cards
        # yet. Match exact names in card headings/accessible labels, not arbitrary
        # substrings of a card that may also contain prices and court counts.
        def normalize(text):
            return re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', text)).strip().casefold()
        def card_name(text):
            text = re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', text)).strip()
            if kind == 'venue':
                # Actual card text may concatenate its preview without whitespace:
                # Kallang Tennis CentreLoading... / CentreSlots availableMon, ...
                # Remove only known status markers; never accept a name prefix.
                text = re.split(r'Loading\.{3}|Slots available(?=(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun|[\s,:]|$))',
                                text, maxsplit=1, flags=re.I)[0].strip()
            return text
        target = normalize(name)
        deadline = time.monotonic() + timeout
        if self.remote_deadline:
            deadline = min(deadline, self.remote_deadline - 15)
        names, hits, seen_names = [], set(), set()
        scroll_started = False
        bottom_geometry = None
        bottom_since = None
        bottom_checks = 0
        # Reaching the current end can start another lazy-loaded batch. Require
        # repeated failed downward scrolls and stable list dimensions before
        # treating that position as the real bottom.
        settle_seconds = min(1.0, max(0.05, timeout / 3))
        self.mark_operation('向下滚动查找' + name[:100], timeout=timeout,
                            search_bottom_confirmed=False)
        while True:
            if not self.is_current():
                raise ScanError('本轮查询已取消。')
            await self.wait_until_ready(page, timeout=5 if self.remote_deadline else 45)
            links = await page.locator('a[href]').evaluate_all("""nodes => nodes.map(n => {
              const walker = document.createTreeWalker(n, NodeFilter.SHOW_TEXT);
              let first;
              while ((first = walker.nextNode())) {
                if (first.textContent.trim() && !first.parentElement.closest(
                    'script,style,svg,[hidden],[aria-hidden="true"]')) break;
              }
              // Follow only the first content node's ancestors. Taking every
              // descendant's text could mistake a nearby-venue note for a title.
              const titles = first ? [first.textContent] : [];
              let parent = first?.parentElement;
              for (let i = 0; parent && parent !== n && i < 6; i++, parent = parent.parentElement) {
                if (parent.textContent.length <= 1000) titles.push(parent.textContent);
              }
              return {href: n.href, rendered: !!([n, ...n.children].some(c => {
                  const box = c.getBoundingClientRect(); return box.width > 0 && box.height > 0;
                }) && getComputedStyle(n).visibility !== 'hidden' && !n.closest('[hidden],[aria-hidden="true"]')),
                labels: [n.querySelector('h1,h2,h3,h4,[role="heading"]')?.textContent || '',
                         n.getAttribute('aria-label') || '', ...titles, (n.textContent || '').slice(0, 1000)]
                  .map(s => s.replace(/\\s+/g, ' ').trim()).filter(Boolean)};
            })""")
            names = []
            for item in links:
                if not item['rendered']:
                    continue
                try:
                    destination = official_url(item['href'])
                except ScanError:
                    continue
                labels = [card_name(label) for label in item['labels']]
                labels = [label for label in labels if label]
                if any(normalize(label) == target for label in labels):
                    hits.add(destination)
                if labels:
                    label = labels[0][:100]
                    if label not in names:
                        names.append(label)
                        if len(seen_names) < 128:
                            seen_names.add(label)
            if time.monotonic() >= deadline:
                break
            if not scroll_started:
                await self.scroll_search(page, reset=True)
                scroll_started = True
            state = await self.scroll_search(page)
            checked_at = time.monotonic()
            if state['at_bottom'] and not state['moved']:
                if bottom_geometry != state['geometry']:
                    bottom_geometry, bottom_since, bottom_checks = state['geometry'], checked_at, 1
                else:
                    bottom_checks += 1
                confirmed = bottom_checks >= 2 and checked_at - bottom_since >= settle_seconds
            else:
                bottom_geometry, bottom_since, bottom_checks = None, None, 0
                confirmed = False
            self.progress['search_bottom_confirmed'] = confirmed
            if confirmed and checked_at <= deadline and hits:
                if len(hits) == 1:
                    return hits.pop()
                break
            # Move through long lists promptly; pause longer at the end so new
            # content can extend the list. No fixed number of scroll attempts.
            pause = min(poll_interval, 0.1) if state['moved'] else poll_interval
            await asyncio.sleep(min(pause, max(0, deadline - time.monotonic())))
        names = list(seen_names)
        words = set(target.split())
        names.sort(key=lambda text: (-len(words.intersection(normalize(text).split())), text.casefold()))
        names = names[:8]
        label = '场馆' if kind == 'venue' else '运动项目'
        if len(hits) > 1:
            text = f'官网出现多个名为「{name}」的{label}链接，无法唯一选择。'
        elif not self.progress.get('search_bottom_confirmed'):
            text = f'滚动查找「{name}」时尚未确认列表到底，已达到本轮等待上限；未采用本轮结果。'
        else:
            text = f'已滚动至列表底部并等待加载，未能找到「{name}」的{label}链接。'
        if names and not hits:
            text += ' 页面可见名称：' + '、'.join(names[:3]) + '。'
        text += ' 发送 /diagnose 查看本轮页面证据。'
        try:
            diagnostic, _ = await asyncio.wait_for(self.inspect_page(page), timeout=3)
        except Exception:
            diagnostic = {'category': 'unavailable', 'label': '未取得页面证据', 'evidence': []}
        diagnostic['lookup'] = {'kind': kind, 'requested': name[:200], 'candidate_names': names,
                                'exact_destinations': len(hits)}
        failure = ScanError(text)
        failure.diagnostic = diagnostic
        raise failure

    async def scan(self, config):
        if self.lock.locked():
            raise ScanError('浏览器正在处理其他操作，请稍后重试。')
        async with self.lock:
            self.busy = True
            self.mark_step('启动浏览器')
            try:
                return await self._scan(config)
            except ScanError as error:
                await self.save_failure(error)
                raise
            except Exception as error:
                from playwright.async_api import TimeoutError as BrowserTimeout
                operation = self.progress.get('operation', '读取页面')
                if isinstance(error, BrowserTimeout):
                    # Classify known actionability hints, never retain raw errors.
                    clues = str(error).lower()
                    reason = ('其他页面元素挡住按钮' if 'intercepts pointer events' in clues else
                              '按钮尚不可见' if 'not visible' in clues else
                              '按钮尚不稳定' if 'not stable' in clues else
                              '在限定时间内未完成此步骤')
                    self.progress['timeout_reason'] = reason
                    message = f'ActiveSG 操作超时：{operation}；{reason}，未采用本轮结果。'
                else:
                    message = f'ActiveSG 页面读取失败（{operation}），未采用本轮结果。'
                failure = ScanError(message + ' 可发送 /diagnose 查看失败证据。')
                await self.save_failure(failure)
                raise failure from None
            finally:
                try:
                    # Keep cookies on disk, but no Chromium/driver between queries.
                    await self.release_browser()
                finally:
                    self.busy = False
                    self.remote_deadline = 0.0
                    self.handoff = None
                    self.progress = {}

    async def click_link(self, page, destination, timeout=15):
        # Click the actual visible card; responsive pages may contain hidden
        # copies with the same href before the visible one.
        destination = official_url(destination)
        links = await page.locator('a[href]').evaluate_all("""nodes => nodes.map(n => ({
            href: n.href, raw: n.getAttribute('href'),
            rendered: !!([n, ...n.children].some(c => {
                const box = c.getBoundingClientRect(); return box.width > 0 && box.height > 0;
            }) && getComputedStyle(n).visibility !== 'hidden' && !n.closest('[hidden],[aria-hidden="true"]'))
        }))""")
        for link in links:
            if link['href'] == destination and link['rendered']:
                target = page.locator('a[href=' + json.dumps(link['raw']) + ']:visible').first
                name = self.progress.get('target') or '页面按钮'
                self.mark_operation('滚动到' + name[:100], timeout=timeout,
                                    target_scrolled=False, clicked=False)
                if not self.is_current():
                    raise ScanError('本轮查询已取消。')
                # Some card links use display:contents, which has no box of its
                # own. The rendered child belongs to the same verified link.
                for _ in range(4):
                    box = await target.bounding_box(timeout=timeout * 1000)
                    if box and box['width'] > 0 and box['height'] > 0:
                        break
                    target = target.locator(':scope > *:visible').first
                await target.scroll_into_view_if_needed(timeout=timeout * 1000)
                self.progress['target_scrolled'] = True
                position = await target.evaluate(TARGET_POSITION)
                self.progress['target_in_view'] = position['in_view']
                self.mark_operation('点击' + name[:100], timeout=timeout)
                if not self.is_current():
                    raise ScanError('本轮查询已取消。')
                await target.click(timeout=timeout * 1000, no_wait_after=True)
                self.mark_operation('等待点击后的页面切换', timeout=timeout, clicked=True)
                # Confirm the route, then let the next reader wait for the
                # required UI. Waiting for load would also wait for every image.
                await page.wait_for_function(r"""expected =>
                    location.origin + location.pathname.replace(/\/$/, '') === expected
                """, arg=safe_url(destination).rstrip('/'), timeout=timeout * 1000)
                self.mark_operation('读取点击后的页面', timeout=15)
                return await self.wait_until_ready(page, timeout=5 if self.remote_deadline else 45)
        raise ScanError('场馆或运动项目按钮已变化，本轮未采用。')

    async def select_day(self, page, day, timeout=8, poll_interval=0.25):
        expected = date.fromisoformat(day).strftime('%a, %d %b').replace(', 0', ', ')
        deadline = time.monotonic() + timeout
        if self.remote_deadline:
            deadline = min(deadline, self.remote_deadline - 15)
        self.mark_operation('查找日期按钮', timeout=timeout)
        locator = page.locator('[aria-label^="View timeslots for"]')
        buttons = []
        while True:
            if not self.is_current():
                raise ScanError('本轮查询已取消。')
            await self.guard(page)
            buttons = await locator.evaluate_all("""nodes => nodes.map(n => ({
              label: n.getAttribute('aria-label') || '',
              selected: n.hasAttribute('data-selected') && n.getAttribute('data-selected') !== 'false'
                || n.getAttribute('aria-pressed') === 'true' || n.getAttribute('aria-selected') === 'true',
              disabled: n.matches(':disabled') || n.getAttribute('aria-disabled') === 'true',
              visible: !!n.getClientRects().length
            }))""")
            hits = [(index, b) for index, b in enumerate(buttons)
                    if b['visible'] and expected in b['label'].replace(', 0', ', ')]
            if len(hits) == 1:
                index, button = hits[0]
                if button['disabled']:
                    return False
                if not button['selected']:
                    self.mark_operation('点击日期按钮', timeout=10, clicked=False)
                    await locator.nth(index).click(timeout=10000, no_wait_after=True)
                    self.progress['clicked'] = True
                break
            if len(hits) > 1:
                raise ScanError('日期按钮不唯一，本轮未采用。')
            if time.monotonic() >= deadline:
                if any(b['visible'] for b in buttons):
                    return False
                raise ScanError('没有读取到日期按钮，本轮未采用。')
            await asyncio.sleep(poll_interval)
        self.mark_operation('确认选中的日期', timeout=timeout)
        while True:
            await self.wait_until_ready(page, timeout=5 if self.remote_deadline else 45)
            buttons = await locator.evaluate_all("""nodes => nodes.map(n => ({
              label: n.getAttribute('aria-label') || '',
              selected: n.hasAttribute('data-selected') && n.getAttribute('data-selected') !== 'false'
                || n.getAttribute('aria-pressed') === 'true' || n.getAttribute('aria-selected') === 'true'
            }))""")
            try:
                confirmed = assert_date(day, page.url, buttons)
            except ScanError:
                confirmed = False
            if confirmed:
                return True
            if not self.is_current():
                raise ScanError('本轮查询已取消。')
            if time.monotonic() >= deadline:
                raise ScanError('点击日期后未能确认日期已切换，本轮未采用。')
            await asyncio.sleep(poll_interval)

    async def read_slot_controls(self, page, day, timeout=8, poll_interval=0.25):
        # Captions and their controls may hydrate after the date has loaded.
        deadline = time.monotonic() + timeout
        if self.remote_deadline:
            deadline = min(deadline, self.remote_deadline - 15)
        self.mark_operation('等待本日时段及分组加载', timeout=timeout)
        snapshot, active, unknown, stale = {}, [], [], 0
        while True:
            if not self.is_current():
                raise ScanError('本轮查询已取消。')
            await self.guard(page)
            snapshot = await page.evaluate(READ_SLOTS)
            active = [c for c in snapshot['controls'] if c['visible']]
            unknown = [c for c in active if not c['bookingType'] and not c['disabled']
                       and c.get('ariaDisabled') != 'true']
            stale = 0
            for control in active:
                if control['bookingType'] != 'instant':
                    continue
                try:
                    stamp = int(control['value'])
                    stamp = stamp / 1000 if stamp > 100_000_000_000 else stamp
                    stale += datetime.fromtimestamp(stamp, SG).date().isoformat() != day
                except (ValueError, TypeError, KeyError, OSError, OverflowError):
                    stale += 1
            if (active and not unknown and not stale and not snapshot.get('loading')
                    and any(c['bookingType'] for c in active)):
                return active
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(poll_interval)
        try:
            diagnostic, _ = await asyncio.wait_for(self.inspect_page(page), timeout=3)
        except Exception:
            diagnostic = {'category': 'unavailable', 'label': '未取得页面证据', 'evidence': []}
        diagnostic['slots'] = {
            'date': day, 'labels': snapshot.get('labels', []), 'headings': snapshot.get('headings', []),
            'visible': len(active), 'instant': sum(c['bookingType'] == 'instant' for c in active),
            'ballot': sum(c['bookingType'] == 'ballot' for c in active),
            'unknown': sum(not c['bookingType'] for c in active), 'enabled_unknown': len(unknown),
            'stale_instant': stale, 'loading': bool(snapshot.get('loading')),
        }
        message = ('没有读取到可见时段控件，不能确认空场情况。' if not active else
                   '已读取时段控件，但未能确认其即时预约或抽签分组，未采用本轮结果。')
        if snapshot.get('loading'):
            message = '页面时段仍在加载，未采用本轮结果。'
        if stale:
            message = '日期按钮已切换，但时段数据日期仍未一致或时间标识无法读取，未采用本轮结果。'
        failure = ScanError(message + ' 发送 /diagnose 查看实际分组文字和控件数量。')
        failure.diagnostic = diagnostic
        raise failure

    async def _scan(self, config):
        page = await self.ensure_page()
        self.mark_step('打开运动项目列表')
        await self.goto(page, ACTIVITIES)
        self.mark_step('查找运动项目', target=config.activity)
        activity_link = await self.find_link(page, config.activity)
        self.mark_step('进入运动项目的场馆列表', target=config.activity)
        await self.click_link(page, activity_link)
        self.mark_step('查找场馆', target=config.venue)
        venue_link = await self.find_link(page, config.venue, kind='venue')
        self.mark_step('进入场馆时段页', target=config.venue)
        await self.click_link(page, venue_link)
        results, found = [], {}
        for day in config.days():
            if not self.is_current():
                raise ScanError('本轮查询已取消。')
            self.mark_step('选择日期', target=config.venue, day=day)
            if not await self.select_day(page, day):
                results.append({'date': day, 'status': 'not_selectable'})
                continue
            text = await self.guard(page)
            for _ in range(8):
                if 'Each slot is 1 hour long' in text:
                    break
                await asyncio.sleep(0.5)
                text = await self.guard(page)
            if config.venue.casefold() not in text.casefold():
                raise ScanError('官网场馆与监控条件不一致，本次查询不采用。')
            if 'Each slot is 1 hour long' not in text:
                raise ScanError('官网一小时预约分组无法确认，本次查询不采用。')
            self.mark_step('读取可选时段', target=config.venue, day=day)
            controls = await self.read_slot_controls(page, day)
            instant = [c for c in controls if c['bookingType'] == 'instant']
            for control in instant:
                slot = slot_from_control(config, day, control)
                if slot:
                    found[slot['key']] = slot
            results.append({'date': day, 'status': 'checked'})
        return {'checked_at': now_sg().isoformat(), 'results': results,
                'matches': sorted(found.values(), key=lambda s: (s['date'], s['start']))}

    async def screenshot(self):
        if self.lock.locked():
            raise ScanError('正在查询，查询完成后可查看浏览器。')
        async with self.lock:
            page = await self.ensure_page()
            self.last_manual_use = time.monotonic()
            if page.url == 'about:blank':
                await page.goto(ACTIVITIES, wait_until='domcontentloaded', timeout=45000)
            try:
                return await page.screenshot(type='jpeg', quality=60)
            finally:
                self.last_manual_use = time.monotonic()

    async def action(self, action, data):
        if self.lock.locked():
            raise ScanError('正在查询，请先 /pause 并等本轮结束。')
        async with self.lock:
            if action == 'close':
                await self.release_browser()
                return
            page = await self.ensure_page()
            self.last_manual_use = time.monotonic()
            if action == 'open':
                await page.goto(ACTIVITIES, wait_until='domcontentloaded', timeout=45000)
            else:
                if urlparse(page.url).hostname != 'activesg.gov.sg':
                    raise ScanError('该入口只允许操作 ActiveSG 页面。')
                if action == 'click':
                    x, y = int(data['x']), int(data['y'])
                    if not 0 <= x <= 1280 or not 0 <= y <= 900:
                        raise ValueError('坐标无效。')
                    await page.mouse.click(x, y)
                elif action == 'type':
                    text = str(data.get('text', ''))
                    if len(text) > 500:
                        raise ValueError('输入过长。')
                    await page.keyboard.insert_text(text)
                elif action == 'key' and data.get('key') in ('Enter', 'Tab', 'Backspace', 'Escape', 'ArrowDown', 'ArrowUp'):
                    await page.keyboard.press(data['key'])
                elif action == 'scroll':
                    await page.mouse.wheel(0, max(-800, min(800, int(data.get('dy', 500)))))
                else:
                    raise ValueError('操作无效。')

    async def close(self):
        async with self.lock:
            await self.release_browser()
