import asyncio
from datetime import date, datetime
import os
from pathlib import Path
import re
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

from models import now_sg, slot_from_control

BASE = 'https://activesg.gov.sg'
ACTIVITIES = BASE + '/facility-bookings/activities'


class ScanError(Exception):
    pass


class VerificationRequired(ScanError):
    pass


class SecurityCheckPending(VerificationRequired):
    pass


def official_url(value, origin=BASE):
    url = urljoin(origin, value)
    parsed = urlparse(url)
    if parsed.scheme != 'https' or parsed.hostname != 'activesg.gov.sg' or not parsed.path.startswith('/facility-bookings/'):
        raise ScanError('官网链接结构发生变化，本次查询不采用。')
    return url


def date_url(url, day):
    parsed = urlparse(official_url(url))
    query = parse_qs(parsed.query)
    query['date'] = [day]
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))


def assert_date(requested, current_url, buttons):
    actual = parse_qs(urlparse(current_url).query).get('date', [''])[0]
    if actual != requested:
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

    async def ensure_page(self):
        if self.page and not self.page.is_closed():
            return self.page
        from playwright.async_api import async_playwright
        if self.playwright is None:
            self.playwright = await async_playwright().start()
        try:
            endpoint = os.getenv('BROWSER_CDP_URL', '')
            if endpoint:
                self.browser = await self.playwright.chromium.connect_over_cdp(endpoint, timeout=20000)
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
            self.page.set_default_timeout(15000)
        except ScanError:
            raise
        except Exception:
            raise ScanError('云端浏览器启动失败，请检查 Chromium 安装、系统依赖和服务内存；详情见浏览器诊断。') from None
        return self.page

    async def guard(self, page):
        title = await page.title()
        text = (await page.locator('body').inner_text())[:20000]
        if re.search(r'just a moment|performing security verification|verify (?:you are|that you)|enable javascript and cookies|checking your browser', title + '\n' + text, re.I):
            raise SecurityCheckPending('ActiveSG 的网站安全检查尚未完成。')
        if re.search(r'singpass|/login|/sign-in', page.url, re.I):
            raise VerificationRequired('ActiveSG 要求登录，请在服务的浏览器页面检查。')
        return text

    async def wait_until_ready(self, page, timeout=45, poll_interval=1):
        # A challenge page can finish its own checks and redirect automatically.
        # Waiting lets the site's scripts run; no checkbox or challenge is automated.
        from playwright.async_api import Error as BrowserError
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            try:
                return await self.guard(page)
            except SecurityCheckPending:
                if asyncio.get_running_loop().time() >= deadline:
                    raise VerificationRequired(
                        'ActiveSG 的网站安全检查等待 45 秒后仍未完成，自动查询未读取到场地数据。'
                        '这不代表查询需要账号认证；可在服务的浏览器页面查看实际拦截页面。') from None
            except BrowserError as error:
                # Automatic navigation can replace the execution context mid-read.
                if 'Execution context was destroyed' not in str(error) or asyncio.get_running_loop().time() >= deadline:
                    raise
            await asyncio.sleep(poll_interval)

    async def goto(self, page, url):
        await page.goto(official_url(url), wait_until='domcontentloaded', timeout=45000)
        await self.wait_until_ready(page)
        try:
            await page.wait_for_load_state('networkidle', timeout=10000)
        except Exception:
            pass
        return await self.wait_until_ready(page)

    async def find_link(self, page, name):
        # Links inside an unexpanded activity accordion can be hidden.
        links = await page.locator('a[href]').evaluate_all('''nodes => nodes.map(n => ({
          text: (n.textContent || '').replace(/\\s+/g, ' ').trim(), href: n.href
        }))''')
        hits = {official_url(item['href']) for item in links if item['text'].casefold() == name.strip().casefold()}
        if len(hits) != 1:
            raise ScanError(f'无法唯一找到「{name}」，请核对官网英文全名，或检查页面是否需要验证。')
        return hits.pop()

    async def scan(self, config):
        if self.lock.locked():
            raise ScanError('浏览器正在处理其他操作，请稍后重试。')
        async with self.lock:
            self.busy = True
            try:
                return await self._scan(config)
            except ScanError:
                raise
            except Exception as error:
                from playwright.async_api import TimeoutError as BrowserTimeout
                if isinstance(error, BrowserTimeout):
                    raise ScanError('ActiveSG 页面加载超时；请检查网站验证以及运动和场馆名称。') from None
                # Do not expose CDP credentials, page HTML, or request URLs in Telegram.
                raise ScanError('ActiveSG 页面读取失败，本轮结果未采用。请检查服务浏览器页面。') from None
            finally:
                self.busy = False

    async def _scan(self, config):
        page = await self.ensure_page()
        await self.goto(page, ACTIVITIES)
        activity_link = await self.find_link(page, config.activity)
        await self.goto(page, activity_link)
        venue_link = await self.find_link(page, config.venue)
        results, found = [], {}
        for day in config.days():
            text = await self.goto(page, date_url(venue_link, day))
            for _ in range(8):
                if 'Each slot is 1 hour long' in text:
                    break
                await asyncio.sleep(0.5)
                text = await self.guard(page)
            if config.venue.casefold() not in text.casefold():
                raise ScanError('官网场馆与监控条件不一致，本次查询不采用。')
            buttons = await page.locator('[aria-label^="View timeslots for"]').evaluate_all('''nodes => nodes.map(n => ({
              label: n.getAttribute('aria-label') || '',
              selected: n.hasAttribute('data-selected') && n.getAttribute('data-selected') !== 'false'
                || n.getAttribute('aria-pressed') === 'true' || n.getAttribute('aria-selected') === 'true'
            }))''')
            if not assert_date(day, page.url, buttons):
                results.append({'date': day, 'status': 'not_selectable'})
                continue
            if 'Each slot is 1 hour long' not in text:
                raise ScanError('官网一小时预约分组无法确认，本次查询不采用。')
            controls = await page.locator('input[type="checkbox"]').evaluate_all('''nodes => nodes.map(n => {
              const label = n.closest('label') || (n.id ? document.querySelector('label[for="' + CSS.escape(n.id) + '"]') : null);
              const display = label || n;
              let container = n.parentElement;
              let bookingType = '';
              while (container && container !== document.body) {
                const heading = container.querySelector('h1,h2,h3,h4,[role="heading"]');
                if (heading && /instant|ballot/i.test(heading.textContent)) { bookingType = heading.textContent.trim(); break; }
                container = container.parentElement;
              }
              return {value:n.value, disabled:n.disabled || n.matches(':disabled'),
                ariaDisabled:n.getAttribute('aria-disabled') || display.getAttribute('aria-disabled'),
                visible:!!(display.getClientRects().length && getComputedStyle(display).visibility !== 'hidden'), bookingType};
            })''')
            if not controls:
                raise ScanError('没有读取到官网时段控件，不能确认空场情况。')
            instant = [c for c in controls if re.search(r'instant', c['bookingType'], re.I) and not re.search(r'ballot', c['bookingType'], re.I)]
            if not instant:
                raise ScanError('无法确认即时预约分组，本次查询不采用。')
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
            if page.url == 'about:blank':
                await page.goto(ACTIVITIES, wait_until='domcontentloaded', timeout=45000)
            return await page.screenshot(type='jpeg', quality=75)

    async def action(self, action, data):
        if self.lock.locked():
            raise ScanError('正在查询，请先 /pause 并等本轮结束。')
        async with self.lock:
            page = await self.ensure_page()
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
        if self.browser:
            await self.browser.close()
        elif self.context:
            await self.context.close()
        if self.playwright:
            await self.playwright.stop()
        self.browser = self.context = self.page = self.playwright = None