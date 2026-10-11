import asyncio
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from engine import Engine
from remote_browser import RemoteProfile, RemoteBrowserError, live_url, provider
from scanner import ACTIVITIES, Scanner, ScanError, VerificationRequired, SecurityCheckPending
from storage import Store
from test_monitor import FakeTelegram, config, result, slot

TOKEN = 'private-test-api-credential-01234567890123456789'
ENV = {'BROWSERLESS_TOKEN': TOKEN, 'BROWSER_PROVIDER': 'browserless', 'BROWSER_CDP_URL': '',
       'BROWSERLESS_REGION': 'sfo', 'BROWSERLESS_TIMEOUT_SECONDS': '120',
       'TELEGRAM_USER_ID': '', 'TELEGRAM_CHAT_ID': ''}


class RemoteProfileTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, ENV))
        self.directory = self.enterContext(tempfile.TemporaryDirectory())

    async def test_reconnect_and_restart_reuse_profile_not_api_call(self):
        calls = []
        def handler(request):
            calls.append(request)
            body = json.loads(request.content)
            self.assertEqual(body['browser'], 'chrome')
            self.assertFalse(body['headless'])
            self.assertFalse(body['stealth'])
            self.assertNotIn('proxy', body)
            self.assertEqual(body['ttl'], 86400000)
            return httpx.Response(200, json={'connect': 'wss://production-sfo.browserless.io/session/id?token=' + TOKEN,
                                           'ttl': 86400000})
        client = httpx.AsyncClient
        with patch('remote_browser.httpx.AsyncClient', side_effect=lambda **kw: client(transport=httpx.MockTransport(handler), **kw)):
            first = await RemoteProfile(self.directory).endpoint()
            second = await RemoteProfile(self.directory).endpoint()
        self.assertEqual(first, second)
        self.assertIn('timeout=120000', first)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].url.params['token'], TOKEN)

    async def test_creation_error_does_not_expose_response_or_token(self):
        for status in (403, 429, 500):
            client = httpx.AsyncClient
            with patch('remote_browser.httpx.AsyncClient', side_effect=lambda **kw: client(
                    transport=httpx.MockTransport(lambda r: httpx.Response(status, text=TOKEN)), **kw)):
                with self.assertRaises(RemoteBrowserError) as error:
                    await RemoteProfile(self.directory).endpoint()
            self.assertNotIn(TOKEN, str(error.exception))

    async def test_missing_token_fails_closed_and_live_link_cannot_contain_api_key(self):
        with patch.dict(os.environ, {'BROWSERLESS_TOKEN': ''}):
            with self.assertRaisesRegex(RemoteBrowserError, '不会改用'):
                await RemoteProfile(self.directory).endpoint()
        for url in ('https://evil.example/live', 'http://production-sfo.browserless.io/live',
                    'https://production-sfo.browserless.io/live?token=' + TOKEN):
            with self.assertRaises(RemoteBrowserError):
                live_url(url, TOKEN)
        self.assertEqual(provider(), 'browserless')
        with patch.dict(os.environ, {'BROWSER_PROVIDER': 'auto'}):
            self.assertEqual(provider(), 'browserless')

    async def test_remote_start_never_launches_local_chromium(self):
        scanner = Scanner(self.directory)
        page = SimpleNamespace(is_closed=lambda: False, url=ACTIVITIES,
                               set_viewport_size=AsyncMock(), on=lambda *a: None, set_default_timeout=lambda *a: None)
        context = SimpleNamespace(pages=[page], close=AsyncMock())
        browser = SimpleNamespace(contexts=[context], close=AsyncMock())
        chromium = SimpleNamespace(connect_over_cdp=AsyncMock(return_value=browser), launch_persistent_context=AsyncMock())
        driver = SimpleNamespace(chromium=chromium, stop=AsyncMock())
        factory = SimpleNamespace(start=AsyncMock(return_value=driver))
        scanner.remote_profile.endpoint = AsyncMock(return_value='wss://production-sfo.browserless.io/session/test')
        with patch('playwright.async_api.async_playwright', return_value=factory):
            self.assertIs(await scanner.ensure_page(), page)
        chromium.launch_persistent_context.assert_not_called()
        await scanner.close()
        browser.close.assert_awaited_once()
        context.close.assert_not_called()


class FakeCDP:
    def __init__(self, seconds=90):
        self.seconds = seconds
        self.events = {}
        self.calls = []
    def on(self, name, handler):
        self.events[name] = handler
    async def send(self, name, params):
        self.calls.append((name, params))
        if name == 'Browserless.liveURL':
            return {'liveURLId': 'vendor-page', 'liveURL': 'https://production-sfo.browserless.io/live/viewer-capability',
                    'timeout': self.seconds * 1000, 'error': None}
        return {'error': None}
    async def detach(self):
        pass
    def complete(self, reason):
        self.events['Browserless.liveComplete']({'liveURLId': 'vendor-page', 'reason': reason})


class HumanTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, ENV))
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.scanner = Scanner(self.directory)
        self.cdp = FakeCDP()
        self.scanner.context = SimpleNamespace(new_cdp_session=AsyncMock(return_value=self.cdp), close=AsyncMock())
        self.page = SimpleNamespace(url=ACTIVITIES)
        self.scanner.remote_deadline = time.monotonic() + 115
        self.pending = SecurityCheckPending('check', {'category': 'cloudflare_challenge'})
        self.scanner.guard = AsyncMock(side_effect=self.pending)
        self.notified = asyncio.Event()
        async def notify(item):
            self.notified.set()
        self.scanner.on_human = notify

    async def test_keeps_session_until_actual_page_then_revokes_url(self):
        task = asyncio.create_task(self.scanner.human_intervention(self.page, self.pending, 0.01))
        await asyncio.wait_for(self.notified.wait(), 1)
        self.assertFalse(task.done())
        item = self.scanner.handoff
        self.assertTrue(self.scanner.valid_handoff(item['id']))
        self.assertIn('/live/', self.scanner.human_message())
        self.assertNotIn(TOKEN, self.scanner.human_message())
        self.scanner.context.close.assert_not_called()
        self.scanner.guard.side_effect = None
        self.scanner.guard.return_value = 'ActiveSG Tennis facilities ' * 10
        self.assertIn('Tennis', await asyncio.wait_for(task, 1))
        self.assertIsNone(self.scanner.handoff)
        self.assertEqual(self.cdp.calls[-1][0], 'Browserless.closeLiveURL')

    async def test_done_is_not_clearance_proof(self):
        task = asyncio.create_task(self.scanner.human_intervention(self.page, self.pending, 0.01))
        await asyncio.wait_for(self.notified.wait(), 1)
        self.cdp.complete('userDone')
        with self.assertRaisesRegex(VerificationRequired, '仍未确认'):
            await asyncio.wait_for(task, 1)
        self.assertIsNone(self.scanner.handoff)

    async def test_blank_page_is_not_clearance_and_timeout_revokes(self):
        self.cdp.seconds = 0.03
        self.scanner.guard.side_effect = None
        self.scanner.guard.return_value = ''
        with self.assertRaisesRegex(VerificationRequired, '已过期'):
            await self.scanner.human_intervention(self.page, self.pending, 0.005)
        self.assertEqual(self.cdp.calls[-1][0], 'Browserless.closeLiveURL')

    async def test_pause_or_condition_change_cancels_handoff(self):
        task = asyncio.create_task(self.scanner.human_intervention(self.page, self.pending, 0.01))
        await asyncio.wait_for(self.notified.wait(), 1)
        self.scanner.is_current = lambda: False
        self.assertFalse(self.scanner.valid_handoff(self.scanner.handoff['id']))
        with self.assertRaisesRegex(VerificationRequired, '已取消'):
            await asyncio.wait_for(task, 1)
        self.assertEqual(self.cdp.calls[-1][0], 'Browserless.closeLiveURL')

    async def test_cancel_task_also_revokes_url(self):
        task = asyncio.create_task(self.scanner.human_intervention(self.page, self.pending, 0.01))
        await asyncio.wait_for(self.notified.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIsNone(self.scanner.handoff)
        self.assertEqual(self.cdp.calls[-1][0], 'Browserless.closeLiveURL')


class HumanTelegramTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, ENV))
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.store = Store(self.directory)
        self.scanner = Scanner(self.directory)
        self.telegram = FakeTelegram()
        self.engine = Engine(self.store, self.scanner, self.telegram, 'activeSG_monitor_bot')
        self.engine.ready = True
        self.store.put('owner', {'user_id': '123', 'chat_id': '123'})
        with self.store.transaction():
            self.engine.change_config(config())
        self.release = asyncio.Event()
        self.started = asyncio.Event()

    async def asyncTearDown(self):
        self.store.close()

    def make_handoff(self, seconds=90):
        self.scanner.handoff = {'id': 'temporary', 'url': 'https://production-sfo.browserless.io/live/viewer',
                                'deadline': time.monotonic() + seconds, 'expires_at': '2099-01-01 00:00:00'}
        return self.scanner.handoff

    async def test_link_sent_by_notice_loop_commands_work_while_waiting(self):
        async def scan(c):
            item = self.make_handoff()
            await self.scanner.on_human(item)
            self.started.set()
            await self.release.wait()
            self.scanner.handoff = None
            return result([slot()])
        self.scanner.scan = scan
        scan_task = asyncio.create_task(self.engine.run_scan())
        notice_task = asyncio.create_task(self.engine.notice_loop())
        try:
            await asyncio.wait_for(self.started.wait(), 1)
            for _ in range(100):
                if self.telegram.messages:
                    break
                await asyncio.sleep(0.01)
            self.assertIn('/live/', self.telegram.messages[0][1])
            self.assertIn('等待人工', self.engine.status())
            update = {'message': {'chat': {'type': 'private', 'id': 123}, 'from': {'id': 123}, 'text': '/help'}}
            with self.store.transaction():
                chat, messages = self.engine.handle_message(update)
            self.assertEqual(chat, '123')
            self.assertIn('/browser', messages[0])
            self.release.set()
            await asyncio.wait_for(scan_task, 1)
        finally:
            for task in (scan_task, notice_task):
                task.cancel()
            await asyncio.gather(scan_task, notice_task, return_exceptions=True)

    async def test_expired_queued_link_is_not_delivered(self):
        self.make_handoff(-1)
        with self.store.transaction():
            self.store.queue(['https://production-sfo.browserless.io/live/expired'], self.engine.runtime()['revision'], 'human:temporary')
        task = asyncio.create_task(self.engine.notice_loop())
        try:
            await asyncio.sleep(0.03)
            self.assertEqual(self.telegram.messages, [])
            self.assertIsNone(self.store.next_notice(self.engine.runtime()['revision']))
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_restart_discards_links_but_keeps_slot_notifications(self):
        revision = self.engine.runtime()['revision']
        with self.store.transaction():
            self.store.queue(['expired link'], revision, 'human:temporary')
            self.store.queue(['slot message'], revision)
        Engine(self.store, Scanner(self.directory), self.telegram, 'activeSG_monitor_bot')
        notice = self.store.next_notice(revision)
        self.assertEqual(notice['text'], 'slot message')




class HumanBrowserDOMTests(unittest.IsolatedAsyncioTestCase):
    async def test_challenge_handoff_telegram_and_complete_scan_on_same_page(self):
        from datetime import datetime, date
        from urllib.parse import urlparse
        from playwright.async_api import async_playwright
        from models import SG
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, ENV):
            store = Store(directory)
            scanner = Scanner(directory)
            telegram = FakeTelegram()
            engine = Engine(store, scanner, telegram, 'activeSG_monitor_bot')
            store.put('owner', {'user_id': '123', 'chat_id': '123'})
            engine.ready = True
            with store.transaction():
                engine.change_config(config())
            c = config()
            stamp = int(datetime.fromisoformat(c.date_from + 'T12:00').replace(tzinfo=SG).timestamp() * 1000)
            label = date.fromisoformat(c.date_from).strftime('%a, %d %b')
            cleared = False
            async with async_playwright() as driver:
                browser = await driver.chromium.launch(headless=True)
                page = await browser.new_page()
                async def fixture(route):
                    path = urlparse(route.request.url).path
                    if path == '/facility-bookings/activities' and not cleared:
                        await route.fulfill(status=403, content_type='text/html', headers={'cf-mitigated': 'challenge'},
                                            body='<title>Just a moment...</title><body>Performing security verification</body>')
                        return
                    if path == '/facility-bookings/activities':
                        html = '<h1>ActiveSG Facility Bookings</h1><p>' + 'Select an activity and venue. ' * 8 + '</p><a href="/facility-bookings/activities/tennis">Tennis</a>'
                    elif path == '/facility-bookings/activities/tennis':
                        html = '<a href="/facility-bookings/venues/kallang">Kallang Tennis Centre</a>'
                    else:
                        html = f'''<h1>Kallang Tennis Centre</h1><p>Each slot is 1 hour long.</p>
                        <button aria-label="View timeslots for {label}" data-selected="true">date</button>
                        <section><h2>Instant booking</h2>
                        <label><input type="checkbox" value="{stamp}">12:00</label>
                        <label><input type="checkbox" value="{stamp+3600000}">13:00</label></section>'''
                    await route.fulfill(status=200, content_type='text/html', body=html)
                await page.route('https://activesg.gov.sg/**', fixture)
                scanner.page = page
                page.on('response', scanner.observe_response)
                cdp = FakeCDP()
                scanner.context = SimpleNamespace(new_cdp_session=AsyncMock(return_value=cdp), close=AsyncMock())
                scanner.remote_deadline = time.monotonic() + 115
                wait = scanner.wait_until_ready
                scanner.wait_until_ready = lambda p, **kw: wait(p, timeout=0.02, poll_interval=0.01)
                query = asyncio.create_task(engine.run_scan())
                notices = asyncio.create_task(engine.notice_loop())
                try:
                    for _ in range(200):
                        if telegram.messages:
                            break
                        await asyncio.sleep(0.01)
                    self.assertIn('/live/', telegram.messages[0][1])
                    self.assertIs(scanner.page, page)
                    self.assertFalse(query.done())
                    self.assertIsNone(engine.runtime()['last'])
                    # Simulated human clearance changes the real page response,
                    # without calling a CAPTCHA solver or accepting a Done event.
                    cleared = True
                    await page.reload(wait_until='domcontentloaded')
                    await asyncio.wait_for(query, 5)
                    self.assertEqual(engine.runtime()['last']['matches'], [slot(), slot(13)])
                    self.assertEqual(engine.runtime()['seen'], [slot()['key'], slot(13)['key']])
                    self.assertIsNone(scanner.handoff)
                    self.assertEqual(cdp.calls[-1][0], 'Browserless.closeLiveURL')
                    self.assertFalse(any('solveCaptcha' in name for name, params in cdp.calls))
                finally:
                    query.cancel()
                    notices.cancel()
                    await asyncio.gather(query, notices, return_exceptions=True)
                    await browser.close()
                    store.close()


if __name__ == '__main__':
    unittest.main()
