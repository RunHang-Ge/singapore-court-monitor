import asyncio
from datetime import timedelta
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, AsyncMock

from engine import Engine
from models import Config, SG, now_sg, parse_set, slot_from_control, summaries
from scanner import Scanner, ScanError, SecurityCheckPending, VerificationRequired, assert_date
from storage import Store
from telegram_api import TelegramError


def config():
    day = (now_sg().date() + timedelta(days=1)).isoformat()
    return Config('Tennis', 'Kallang Tennis Centre', day, day, '12:00', '14:00')


def slot(hour=12):
    c = config()
    return {'date': c.date_from, 'start': f'{hour:02}:00', 'end': f'{hour+1:02}:00', 'key': f'{c.date_from}|{hour:02}:00'}


def result(slots):
    return {'checked_at': now_sg().isoformat(), 'results': [{'date': config().date_from, 'status': 'checked'}], 'matches': slots}


class FakeTelegram:
    token = 'test'
    def __init__(self):
        self.messages = []
        self.fail = False
    async def send(self, chat_id, text):
        if self.fail:
            raise TelegramError('网络失败')
        self.messages.append((chat_id, text))
        return {'message_id': len(self.messages)}
    async def close(self):
        pass


class FakeScanner:
    busy = False
    def __init__(self):
        self.value = result([slot()])
        self.error = None
        self.started = self.release = None
    async def scan(self, c):
        if self.started:
            self.started.set()
            await self.release.wait()
        if self.error:
            raise self.error
        return self.value
    async def close(self):
        pass


class ModelTests(unittest.TestCase):
    def test_complete_hour_and_singapore_boundary(self):
        c = config()
        stamp = __import__('datetime').datetime.fromisoformat(c.date_from + 'T12:00').replace(tzinfo=SG).timestamp() * 1000
        item = {'visible': True, 'disabled': False, 'value': str(int(stamp))}
        self.assertEqual(slot_from_control(c, c.date_from, item), slot())
        self.assertIsNone(slot_from_control(c, c.date_from, {**item, 'disabled': True}))
        self.assertIsNone(slot_from_control(c, c.date_from, {**item, 'visible': False}))
        with self.assertRaisesRegex(ValueError, '其他日期'):
            slot_from_control(c, (now_sg().date() + timedelta(days=2)).isoformat(), item)

    def test_12_hour_input_and_invalid_config(self):
        c = config()
        parsed = parse_set(f'Tennis / Kallang Tennis Centre / {c.date_from} / 7pm-10pm')
        self.assertEqual((parsed.time_from, parsed.time_to), ('19:00', '22:00'))
        with self.assertRaises(ValueError):
            parse_set(f'Tennis / Kallang Tennis Centre / {c.date_from} / 12:00-12:30')
        with self.assertRaises(ValueError):
            parse_set('Tennis / Kallang Tennis Centre / 2026-02-30 / 12:00-14:00')
        with self.assertRaises(ValueError):
            parse_set('Tennis / Kallang Tennis Centre / 2020-01-01 / 12:00-14:00')

    def test_date_lines_deduplicate_and_no_links(self):
        a, b = slot(), slot(13)
        messages = summaries([b, a, a, {**a, 'date': '2099-12-31', 'key': 'other'}])
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].split('；\n')[0], a['date'] + ' 12:00–13:00、13:00–14:00')
        self.assertNotIn('http', messages[0])

    def test_long_summaries_fit_telegram(self):
        slots = []
        for day in range(1, 32):
            for hour in range(23):
                slots.append({'date': f'2099-01-{day:02}', 'start': f'{hour:02}:00', 'end': f'{hour+1:02}:00'})
        self.assertTrue(all(len(m) < 4096 for m in summaries(slots)))
        self.assertEqual(sum(len(m.split('；\n')) for m in summaries(slots)), 31)

    def test_requested_date_is_verified(self):
        c = config()
        label = 'View timeslots for ' + __import__('datetime').date.fromisoformat(c.date_from).strftime('%a, %d %b')
        self.assertTrue(assert_date(c.date_from, 'https://activesg.gov.sg/facility-bookings/x?date=' + c.date_from, [{'label': label, 'selected': True}]))
        self.assertFalse(assert_date(c.date_from, 'https://activesg.gov.sg/facility-bookings/x?date=2020-01-01', [{'label': label, 'selected': True}]))
        with self.assertRaises(ScanError):
            assert_date(c.date_from, 'https://activesg.gov.sg/facility-bookings/x?date=' + c.date_from, [])


class EngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(self.directory.name)
        self.scanner, self.telegram = FakeScanner(), FakeTelegram()
        with patch.dict(os.environ, {'TELEGRAM_USER_ID': '', 'TELEGRAM_CHAT_ID': ''}):
            self.engine = Engine(self.store, self.scanner, self.telegram, 'activeSG_monitor_bot', binding_code='x' * 32)
        self.engine.ready = True
        self.store.put('owner', {'user_id': '123', 'chat_id': '123'})
        with self.store.transaction():
            self.engine.change_config(config())

    async def asyncTearDown(self):
        self.store.close()
        self.directory.cleanup()

    def messages(self):
        return [r[0] for r in self.store.db.execute('SELECT text FROM outbox WHERE kind="slots" ORDER BY id')]

    async def test_only_new_slots_and_reappearing_slots(self):
        await self.engine.run_scan()
        self.assertEqual(self.messages(), summaries([slot()]))
        with self.store.transaction():
            self.store.db.execute('DELETE FROM outbox')
        self.scanner.value = result([slot(), slot(13)])
        await self.engine.run_scan()
        self.assertEqual(self.messages(), summaries([slot(13)]))
        with self.store.transaction():
            self.store.db.execute('DELETE FROM outbox')
        await self.engine.run_scan()
        self.assertEqual(self.messages(), [])
        self.scanner.value = result([])
        await self.engine.run_scan()
        self.assertEqual(self.messages(), [])
        self.scanner.value = result([slot()])
        await self.engine.run_scan()
        self.assertEqual(self.messages(), summaries([slot()]))

    async def test_failed_query_does_not_erase_baseline(self):
        await self.engine.run_scan()
        before = self.engine.runtime()
        self.scanner.error = VerificationRequired('网站验证')
        await self.engine.run_scan()
        await self.engine.run_scan()
        after = self.engine.runtime()
        self.assertEqual(after['seen'], before['seen'])
        self.assertEqual(after['last'], before['last'])
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM outbox WHERE kind="status"').fetchone()[0], 1)

    async def test_changed_condition_discards_inflight_result(self):
        self.scanner.started, self.scanner.release = asyncio.Event(), asyncio.Event()
        work = asyncio.create_task(self.engine.run_scan())
        await self.scanner.started.wait()
        with self.store.transaction():
            self.engine.change_config(__import__('dataclasses').replace(config(), time_from='19:00', time_to='22:00'))
        self.scanner.release.set()
        await work
        self.assertIsNone(self.engine.runtime()['last'])
        self.assertEqual(self.messages(), [])

    async def test_notifications_survive_restart_and_failed_send(self):
        await self.engine.run_scan()
        self.telegram.fail = True
        work = asyncio.create_task(self.engine.notice_loop())
        await asyncio.sleep(0.05)
        work.cancel()
        await asyncio.gather(work, return_exceptions=True)
        self.assertEqual(len(self.messages()), 1)
        self.store.close()
        self.store = Store(self.directory.name)
        self.engine.store = self.store
        self.telegram.fail = False
        work = asyncio.create_task(self.engine.notice_loop())
        for _ in range(20):
            if self.telegram.messages:
                break
            await asyncio.sleep(0.02)
        work.cancel()
        await asyncio.gather(work, return_exceptions=True)
        self.assertEqual(len(self.telegram.messages), 1)
        self.assertEqual(self.messages(), [])
        self.assertEqual(self.engine.runtime()['seen'], [slot()['key']])

    async def test_private_owner_commands_and_results(self):
        def message(text, user=123):
            return {'message': {'chat': {'type': 'private', 'id': user}, 'from': {'id': user}, 'text': text}}
        self.assertEqual(self.engine.handle_message(message('/pause', 999)), (None, None))
        with self.store.transaction():
            _, reply = self.engine.handle_message(message('/pause'))
        self.assertFalse(self.engine.runtime()['enabled'])
        with self.store.transaction():
            self.engine.handle_message(message('/time 7pm-10pm'))
        self.assertFalse(self.engine.runtime()['enabled'])
        self.assertIn('19:00', self.engine.config().text())
        self.assertEqual(self.engine.handle_message(message('/status@other_bot')), (None, None))
        self.assertIn('可用指令', self.engine.handle_message(message('/help'))[1][0])
        self.scanner.last_failure = lambda: None
        self.assertIn('尚未保存', self.engine.handle_message(message('/diagnose'))[1][0])
        self.assertEqual(self.engine.handle_message(message('/diagnose', 999)), (None, None))

    async def test_binding_requires_secret(self):
        self.store.put('owner', None)
        msg = {'message': {'chat': {'type': 'private', 'id': 123}, 'from': {'id': 123}, 'text': '/start wrong'}}
        self.assertEqual(self.engine.handle_message(msg), (None, None))
        msg['message']['text'] = '/start ' + 'x' * 32
        with self.store.transaction():
            self.engine.handle_message(msg)
        self.assertEqual(self.store.get('owner')['user_id'], '123')

    async def test_duplicate_update_is_applied_once(self):
        msg = {'update_id': 55, 'message': {'chat': {'type': 'private', 'id': 123}, 'from': {'id': 123}, 'text': '/time 19:00-22:00'}}
        self.store.accept_update(msg)
        self.store.accept_update(msg)
        work = asyncio.create_task(self.engine.update_loop())
        for _ in range(30):
            if self.telegram.messages:
                break
            await asyncio.sleep(0.02)
        work.cancel()
        await asyncio.gather(work, return_exceptions=True)
        self.assertEqual(len(self.telegram.messages), 1)
        self.assertEqual(self.engine.runtime()['revision'], 2)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM updates WHERE done=1').fetchone()[0], 1)


class WebTests(unittest.TestCase):
    def test_authentication_webhook_secret_and_queue(self):
        from fastapi.testclient import TestClient
        from main import app
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'DATA_DIR': directory, 'ADMIN_PASSWORD': 'test-password-123456', 'TELEGRAM_BOT_TOKEN': '', 'TELEGRAM_USER_ID': '', 'TELEGRAM_CHAT_ID': ''}):
            with TestClient(app) as client:
                self.assertEqual(client.get('/healthz').status_code, 200)
                self.assertEqual(client.get('/').status_code, 401)
                self.assertEqual(client.get('/browser/diagnostic').status_code, 401)
                self.assertEqual(client.get('/browser/diagnostic/screenshot').status_code, 401)
                auth = ('admin', 'test-password-123456')
                self.assertEqual(client.get('/', auth=auth).status_code, 200)
                state = client.get('/status', auth=auth).json()
                self.assertIn('/start ', state['binding_command'])
                self.assertIn('container_mib', state['memory'])
                self.assertFalse(state['browser_active'])
                self.assertIn('尚未保存', client.get('/browser/diagnostic', auth=auth).json()['text'])
                self.assertEqual(client.get('/browser/diagnostic/screenshot', auth=auth).status_code, 404)
                self.assertFalse(client.get('/status', auth=auth).json()['browser_active'])
                self.assertEqual(client.post('/browser/action', auth=auth, json={'action': 'open'}).status_code, 403)
                update = {'update_id': 123, 'message': {'text': '/help'}}
                self.assertEqual(client.post('/telegram/webhook', json=update).status_code, 403)
                headers = {'x-telegram-bot-api-secret-token': client.portal.call(lambda: app.state.engine.store.get('webhook_secret'))}
                self.assertEqual(client.post('/telegram/webhook', headers=headers, json=update).status_code, 200)
                self.assertEqual(client.post('/telegram/webhook', headers=headers, json=update).status_code, 200)
                self.assertEqual(client.portal.call(lambda: app.state.engine.store.db.execute('SELECT COUNT(*) FROM updates').fetchone()[0]), 1)


class ReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def test_automatic_security_check_can_finish(self):
        scanner = Scanner('.')
        scanner.guard = AsyncMock(side_effect=[SecurityCheckPending('checking'), 'available page'])
        self.assertEqual(await scanner.wait_until_ready(object(), timeout=1, poll_interval=0.01), 'available page')

    async def test_persistent_security_check_is_reported_after_wait(self):
        scanner = Scanner('.')
        scanner.guard = AsyncMock(side_effect=SecurityCheckPending('checking'))
        with self.assertRaisesRegex(VerificationRequired, '仍未完成'):
            await scanner.wait_until_ready(object(), timeout=0.03, poll_interval=0.01)
        self.assertGreater(scanner.guard.await_count, 1)

    async def test_real_login_requirement_is_not_retried_as_security_check(self):
        scanner = Scanner('.')
        scanner.guard = AsyncMock(side_effect=VerificationRequired('login required'))
        with self.assertRaisesRegex(VerificationRequired, 'login required'):
            await scanner.wait_until_ready(object(), timeout=1, poll_interval=0.01)
        self.assertEqual(scanner.guard.await_count, 1)


class BrowserFixtureTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_dom_instant_only_and_disabled_controls(self):
        from playwright.async_api import async_playwright
        from urllib.parse import urlparse
        c = config()
        stamp = int(__import__('datetime').datetime.fromisoformat(c.date_from + 'T12:00').replace(tzinfo=SG).timestamp() * 1000)
        label = __import__('datetime').date.fromisoformat(c.date_from).strftime('%a, %d %b')
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            async def fixture(route):
                path = urlparse(route.request.url).path
                if path == '/facility-bookings/activities':
                    html = '<a href="/facility-bookings/activities/tennis">Tennis</a>'
                elif path == '/facility-bookings/activities/tennis':
                    html = '<a href="/facility-bookings/venues/kallang">Kallang Tennis Centre</a>'
                else:
                    html = f'''<h1>Kallang Tennis Centre</h1><p>Each slot is 1 hour long.</p>
                    <button aria-label="View timeslots for {label}" data-selected="true">date</button>
                    <section><h2>Instant booking</h2>
                    <label><input type="checkbox" value="{stamp}">12:00</label>
                    <label><input type="checkbox" value="{stamp}">same time, another court</label>
                    <label><input type="checkbox" value="{stamp+3600000}" disabled>13:00</label></section>
                    <section><h2>Ballot</h2><label><input type="checkbox" value="{stamp+3600000}">13:00</label></section>'''
                await route.fulfill(status=200, content_type='text/html', body=html)
            await page.route('https://activesg.gov.sg/**', fixture)
            scanner = Scanner('.')
            scanner.page = page
            value = await scanner.scan(c)
            self.assertEqual(value['matches'], [slot()])
            await page.set_content('<title>Just a moment...</title><p>Performing security verification</p>')
            with self.assertRaises(VerificationRequired):
                await scanner.guard(page)
            await browser.close()



class MemoryTests(unittest.TestCase):
    def test_container_total_limit_and_python_are_distinct(self):
        from memory_usage import snapshot
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, value in [('memory.current', 300 * 1024 ** 2),
                                ('memory.max', 512 * 1024 ** 2),
                                ('memory.peak', 450 * 1024 ** 2)]:
                (root / name).write_text(str(value))
            (root / 'memory.events').write_text('oom 1\noom_kill 1\n')
            (root / 'process').write_text('VmRSS:   51200 kB\n')
            self.assertEqual(snapshot(root, root / 'process'),
                             {'container_mib': 300.0, 'limit_mib': 512.0,
                              'peak_mib': 450.0, 'python_mib': 50.0, 'oom_kills': 1})

    def test_missing_and_unlimited_metrics_do_not_break_service(self):
        from memory_usage import snapshot
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'memory.max').write_text('max')
            self.assertTrue(all(value is None for value in snapshot(root, root / 'missing').values()))

    def test_cgroup_v1_fallback(self):
        from memory_usage import snapshot
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'memory').mkdir()
            (root / 'memory' / 'memory.usage_in_bytes').write_text(str(200 * 1024 ** 2))
            (root / 'memory' / 'memory.limit_in_bytes').write_text(str(2 ** 63 - 4096))
            values = snapshot(root, root / 'missing')
            self.assertEqual(values['container_mib'], 200.0)
            self.assertIsNone(values['limit_mib'])


class BrowserLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def resources(self):
        from types import SimpleNamespace
        scanner = Scanner(self.enterContext(tempfile.TemporaryDirectory()))
        scanner.context = SimpleNamespace(close=AsyncMock())
        scanner.playwright = SimpleNamespace(stop=AsyncMock())
        return scanner, scanner.context, scanner.playwright

    async def test_query_success_error_and_cancellation_all_release(self):
        for failure in [None, ScanError('unavailable'), asyncio.CancelledError()]:
            with self.subTest(failure=type(failure).__name__):
                scanner, context, driver = self.resources()
                expected = result([])
                scanner._scan = AsyncMock(return_value=expected, side_effect=failure)
                if failure is None:
                    self.assertEqual(await scanner.scan(config()), expected)
                else:
                    with self.assertRaises(type(failure)):
                        await scanner.scan(config())
                context.close.assert_awaited_once()
                driver.stop.assert_awaited_once()
                self.assertIsNone(scanner.context)
                self.assertIsNone(scanner.playwright)
                self.assertFalse(scanner.busy)

    async def test_driver_stops_even_if_context_cleanup_fails(self):
        scanner, context, driver = self.resources()
        context.close.side_effect = RuntimeError('closed process')
        await scanner.close()
        driver.stop.assert_awaited_once()
        self.assertFalse(scanner.browser_active)

    async def test_idle_cleanup_keeps_recent_manual_session_and_active_query(self):
        import time
        scanner, context, driver = self.resources()
        scanner.last_manual_use = time.monotonic()
        await scanner.release_if_idle()
        context.close.assert_not_awaited()
        scanner.last_manual_use -= 121
        async with scanner.lock:
            await scanner.release_if_idle()
        context.close.assert_not_awaited()
        await scanner.release_if_idle()
        context.close.assert_awaited_once()
        driver.stop.assert_awaited_once()

    async def test_manual_close_never_reopens_browser(self):
        scanner, context, driver = self.resources()
        scanner.ensure_page = AsyncMock()
        await scanner.action('close', {})
        scanner.ensure_page.assert_not_awaited()
        context.close.assert_awaited_once()

    async def test_cdp_disconnect_does_not_close_borrowed_context(self):
        from types import SimpleNamespace
        scanner, context, driver = self.resources()
        browser = scanner.browser = SimpleNamespace(close=AsyncMock())
        await scanner.close()
        browser.close.assert_awaited_once()
        context.close.assert_not_awaited()
        driver.stop.assert_awaited_once()

    async def test_stale_context_and_failed_launch_release_drivers(self):
        from types import SimpleNamespace
        scanner, old_context, old_driver = self.resources()
        scanner.page = SimpleNamespace(is_closed=lambda: True)
        new_driver = SimpleNamespace(
            chromium=SimpleNamespace(launch_persistent_context=AsyncMock(side_effect=RuntimeError('failed'))),
            stop=AsyncMock())
        factory = SimpleNamespace(start=AsyncMock(return_value=new_driver))
        with tempfile.TemporaryDirectory() as directory:
            scanner.directory = Path(directory)
            with patch('playwright.async_api.async_playwright', return_value=factory), patch.dict(os.environ, {'BROWSER_CDP_URL': ''}):
                with self.assertRaises(ScanError):
                    await scanner.ensure_page()
        old_context.close.assert_awaited_once()
        old_driver.stop.assert_awaited_once()
        new_driver.stop.assert_awaited_once()
        self.assertIsNone(scanner.playwright)

    async def test_real_browser_closes_and_cookies_survive_next_query(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'BROWSER_CDP_URL': '', 'BROWSER_HEADLESS': 'true'}):
            scanner = Scanner(directory)
            pages = []
            async def query(config):
                page = await scanner.ensure_page()
                pages.append(page)
                cookies = await scanner.context.cookies('https://activesg.gov.sg')
                if len(pages) == 1:
                    await scanner.context.add_cookies([{'name': 'monitor_test', 'value': 'saved',
                                                       'domain': 'activesg.gov.sg', 'path': '/',
                                                       'expires': __import__('time').time() + 3600}])
                else:
                    self.assertTrue(any(item['name'] == 'monitor_test' for item in cookies))
                await page.set_content('<p>offline lifecycle fixture</p>')
                return result([])
            scanner._scan = query
            try:
                await scanner.scan(config())
                self.assertTrue(pages[0].is_closed())
                self.assertIsNone(scanner.playwright)
                await scanner.scan(config())
                self.assertTrue(pages[1].is_closed())
                self.assertFalse(scanner.browser_active)
            finally:
                await scanner.close()



class QueryDiagnosisTests(unittest.TestCase):
    def inspect(self, title='ActiveSG', text='Tennis', headers=None, markers=None, url=None):
        from query_diagnostics import classify
        return classify(url or 'https://activesg.gov.sg/facility-bookings/activities',
                        title, text, {'status': 200, 'headers': headers or {}}, markers)

    def test_cf_mitigated_proves_cloudflare_challenge(self):
        value = self.inspect(headers={'cf-mitigated': 'challenge', 'set-cookie': 'secret'})
        self.assertEqual(value['category'], 'cloudflare_challenge')
        self.assertNotIn('set-cookie', value['headers'])

    def test_cf_ray_alone_is_not_proof_of_block(self):
        self.assertEqual(self.inspect(headers={'cf-ray': 'sample-SIN', 'server': 'cloudflare'})['category'], 'page_loaded')

    def test_unidentified_check_and_blank_are_distinct(self):
        self.assertEqual(self.inspect('Just a moment...', 'Performing security verification')['category'], 'unknown_security')
        self.assertEqual(self.inspect('', '')['category'], 'blank')

    def test_cloudflare_dom_markers_and_embedded_widget_are_distinct(self):
        self.assertEqual(self.inspect('Just a moment...', 'Checking your browser',
                                     markers={'challenge_options': True})['category'], 'cloudflare_challenge')
        self.assertEqual(self.inspect('Verify you are human', 'Verify you are human',
                                     markers={'widget': True})['category'], 'cloudflare_widget')
        self.assertEqual(self.inspect('Just a moment...', 'Cloudflare verification')['category'], 'cloudflare_likely')

    def test_login_requires_actual_login_path_or_singpass_domain(self):
        value = self.inspect(url='https://login.singpass.gov.sg/?code=secret')
        self.assertEqual(value['category'], 'login')
        value = self.inspect(text='Log in with Singpass',
                             url='https://activesg.gov.sg/facility-bookings/activities?next=/login')
        self.assertEqual(value['category'], 'page_loaded')
        self.assertNotIn('next', value['url'])

    def test_scripts_on_normal_page_are_not_challenge_proof(self):
        self.assertEqual(self.inspect(markers={'challenge_script': True})['category'], 'page_loaded')

    def test_diagnostic_urls_strip_credentials_queries_and_fragments(self):
        from query_diagnostics import safe_url
        self.assertEqual(safe_url('https://admin:secret@activesg.gov.sg/facility-bookings/activities?token=secret#secret'),
                         'https://activesg.gov.sg/facility-bookings/activities')


class FailureEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_response_and_failure_image_are_saved_before_cleanup(self):
        from playwright.async_api import async_playwright
        from query_diagnostics import format_failure
        import hashlib
        directory = self.enterContext(tempfile.TemporaryDirectory())
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(headless=True)
            scanner = Scanner(directory)
            scanner.page = page = await browser.new_page()
            page.on('response', scanner.observe_response)
            async def challenge(route):
                await route.fulfill(status=403, headers={'cf-mitigated': 'challenge',
                                                        'cf-ray': 'test-SIN',
                                                        'set-cookie': 'must-not-be-saved=secret'},
                                    content_type='text/html',
                                    body='<title>Just a moment...</title><p>Performing security verification</p>')
            await page.route('https://activesg.gov.sg/**', challenge)
            async def query(config):
                await page.goto('https://activesg.gov.sg/facility-bookings/activities')
                await scanner.wait_until_ready(page, timeout=0.03, poll_interval=0.01)
            scanner._scan = query
            try:
                with self.assertRaisesRegex(VerificationRequired, 'Cloudflare'):
                    await scanner.scan(config())
                self.assertIsNone(scanner.page)
                value = scanner.last_failure()
                self.assertEqual(value['category'], 'cloudflare_challenge')
                self.assertEqual(value['http_status'], 403)
                self.assertEqual(value['headers']['cf-mitigated'], 'challenge')
                self.assertNotIn('set-cookie', value['headers'])
                image = (Path(directory) / 'last-query-failure.jpg').read_bytes()
                self.assertEqual(hashlib.sha256(image).hexdigest(), value['screenshot_sha256'])
                self.assertNotIn('https://', format_failure(value))
                scanner.ensure_page = AsyncMock(side_effect=AssertionError('must not reopen browser'))
                self.assertEqual(scanner.last_failure(), value)
                scanner.ensure_page.assert_not_awaited()
            finally:
                await browser.close()

    async def test_login_page_is_never_screenshotted(self):
        from types import SimpleNamespace
        from query_diagnostics import classify
        with tempfile.TemporaryDirectory() as directory:
            scanner = Scanner(directory)
            screenshot = AsyncMock()
            scanner.page = SimpleNamespace(url='https://activesg.gov.sg/login', screenshot=screenshot)
            value = classify(scanner.page.url, 'Log in', 'Log in using Singpass')
            await scanner.save_failure(VerificationRequired('Login required', value))
            screenshot.assert_not_awaited()
            self.assertIsNone(scanner.last_failure()['screenshot_sha256'])


if __name__ == '__main__':
    unittest.main()