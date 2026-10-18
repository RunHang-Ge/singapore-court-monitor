import asyncio
from datetime import timedelta
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from engine import Engine
from scanner import Scanner, ScanError, ACTIVITIES
from storage import Store
from test_monitor import FakeScanner, FakeTelegram, config, result, slot


def message(text):
    return {'message': {'chat': {'type': 'private', 'id': 123}, 'from': {'id': 123}, 'text': text}}


class QueryFeedbackTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {'TELEGRAM_USER_ID': '', 'TELEGRAM_CHAT_ID': '',
                                                'BROWSER_PROVIDER': 'browserless', 'BROWSERLESS_TOKEN': 'test-only'}))
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.store = Store(directory)
        self.addCleanup(self.store.close)
        self.scanner, self.telegram = FakeScanner(), FakeTelegram()
        self.engine = Engine(self.store, self.scanner, self.telegram, 'activeSG_monitor_bot')
        self.engine.ready = True
        self.store.put('owner', {'user_id': '123', 'chat_id': '123'})
        with self.store.transaction():
            self.engine.change_config(config())

    def request(self, command='/browser'):
        with self.store.transaction():
            return self.engine.handle_message(message(command))[1][0]

    def notices(self, kind=None):
        rows = self.store.db.execute('SELECT text, kind FROM outbox ORDER BY id').fetchall()
        return [row['text'] for row in rows if kind is None or row['kind'] == kind]

    def clear_notices(self):
        with self.store.transaction():
            self.store.db.execute('DELETE FROM outbox')

    async def test_every_manual_retry_reports_failure_even_if_already_notified(self):
        self.scanner.error = ScanError('无法唯一找到「Kallang Tennis Centre」。')
        await self.engine.run_scan()
        self.clear_notices()
        for _ in range(2):
            self.request()
            await self.engine.run_scan()
            self.assertEqual(len(self.notices('manual')), 1)
            self.assertIn('Kallang Tennis Centre', self.notices('manual')[0])
            self.assertIsNone(self.store.get('manual_query'))
            self.clear_notices()

    async def test_results_returns_actual_failure_instead_of_wait_forever(self):
        self.scanner.error = ScanError('场馆列表未能加载。')
        await self.engine.run_scan()
        reply = self.engine.full_results()[0]
        self.assertIn('场馆列表未能加载', reply)
        self.assertNotIn('请稍后', reply)

    async def test_manual_success_without_new_slots_still_confirms_completion(self):
        await self.engine.run_scan()
        self.clear_notices()
        self.request('/check')
        await self.engine.run_scan()
        self.assertEqual(self.notices('slots'), [])
        self.assertEqual(len(self.notices('manual')), 1)
        self.assertIn('已完成', self.notices('manual')[0])
        self.assertIn('新增或重新出现 0 个', self.notices('manual')[0])

    async def test_request_during_scan_attaches_to_current_query(self):
        self.scanner.error = ScanError('远程连接失败。')
        self.scanner.started, self.scanner.release = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(self.engine.run_scan())
        try:
            await self.scanner.started.wait()
            reply = self.request()
            self.assertIn('正在查询', reply)
            self.scanner.release.set()
            await task
            self.assertEqual(len(self.notices('manual')), 1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_automatic_repeated_errors_stay_quiet(self):
        self.scanner.error = ScanError('官网检查失败。')
        await self.engine.run_scan()
        self.clear_notices()
        await self.engine.run_scan()
        self.assertEqual(self.notices(), [])

    async def test_resending_live_link_also_requests_failure_outcome(self):
        self.scanner.handoff = {'id': 'live'}
        self.scanner.valid_handoff = lambda identifier: identifier == 'live'
        self.scanner.human_message = lambda: 'temporary viewer link'
        self.request()
        self.scanner.handoff = None
        self.scanner.error = ScanError('人工处理已过期。')
        await self.engine.run_scan()
        self.assertEqual(len(self.notices('manual')), 1)
        self.assertIn('已过期', self.notices('manual')[0])

    async def test_pending_manual_request_survives_restart(self):
        self.request()
        self.engine = Engine(self.store, self.scanner, self.telegram, 'activeSG_monitor_bot')
        self.engine.ready = True
        self.scanner.error = ScanError('启动失败。')
        await self.engine.run_scan()
        self.assertEqual(len(self.notices('manual')), 1)

    async def test_changed_conditions_invalidate_old_manual_request(self):
        self.request()
        from dataclasses import replace
        with self.store.transaction():
            self.engine.change_config(replace(config(), time_from='19:00', time_to='22:00'))
        self.scanner.error = ScanError('旧条件失败。')
        await self.engine.run_scan()
        self.assertEqual(self.notices('manual'), [])


class VenueLoadingTests(unittest.IsolatedAsyncioTestCase):
    async def test_observed_card_text_status_is_not_part_of_venue_name(self):
        from playwright.async_api import async_playwright
        directory = self.enterContext(tempfile.TemporaryDirectory())
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                scanner = Scanner(directory)
                scanner.page = page
                for card in (
                    '<div>Kallang Tennis Centre</div><div>Loading... ... ... ...</div>',
                    'Kallang Tennis CentreLoading... ... ... ...',
                    'Kallang Tennis CentreSlots availableMon, 12 Oct, 11:00 am',
                ):
                    async def fixture(route):
                        await route.fulfill(content_type='text/html', body='<h1>Tennis</h1><a href="/facility-bookings/venues/kallang">' + card + '</a>')
                    await page.route('https://activesg.gov.sg/**', fixture)
                    await page.goto(ACTIVITIES)
                    self.assertEqual(await scanner.find_link(page, 'Kallang Tennis Centre', kind='venue', timeout=1, poll_interval=0.02),
                                     'https://activesg.gov.sg/facility-bookings/venues/kallang')
                    await page.unroute('https://activesg.gov.sg/**', fixture)
            finally:
                await browser.close()

    async def test_status_cleanup_does_not_select_similarly_named_venue(self):
        from playwright.async_api import async_playwright
        directory = self.enterContext(tempfile.TemporaryDirectory())
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.route('https://activesg.gov.sg/**', lambda route: route.fulfill(content_type='text/html', body='''
                  <h1>Tennis</h1>
                  <a href="/facility-bookings/venues/annex"><div>Kallang Tennis Centre Annex</div><div>Loading...</div></a>
                  <a href="/facility-bookings/venues/other"><div>Other Tennis Centre</div><p>Nearby: <span>Kallang Tennis Centre</span></p></a>'''))
                await page.goto(ACTIVITIES)
                scanner = Scanner(directory)
                scanner.page = page
                with self.assertRaises(ScanError):
                    await scanner.find_link(page, 'Kallang Tennis Centre', kind='venue', timeout=0)
            finally:
                await browser.close()

    async def test_diagnostic_names_omit_loading_and_availability_footer(self):
        from playwright.async_api import async_playwright
        from query_diagnostics import format_failure
        directory = self.enterContext(tempfile.TemporaryDirectory())
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.route('https://activesg.gov.sg/**', lambda route: route.fulfill(content_type='text/html', body='''
                    <h1>Tennis</h1>
                    <a href="/facility-bookings/venues/kallang">Kallang Tennis CentreLoading... ... ... ...</a>
                    <a href="/facility-bookings/venues/burghley">Burghley Tennis CentreSlots availableMon, 12 Oct, 11:00 am</a>'''))
                await page.goto(ACTIVITIES)
                scanner = Scanner(directory)
                scanner.page = page
                with self.assertRaises(ScanError) as caught:
                    await scanner.find_link(page, 'Missing Tennis Centre', kind='venue', timeout=0)
                await scanner.save_failure(caught.exception)
                diagnostic = scanner.last_failure()
                self.assertEqual(set(diagnostic['lookup']['candidate_names']),
                                 {'Kallang Tennis Centre', 'Burghley Tennis Centre'})
                self.assertNotIn('Loading', format_failure(diagnostic))
                self.assertNotIn('Slots available', format_failure(diagnostic))
            finally:
                await browser.close()

    async def test_delayed_card_heading_is_matched_without_fuzzy_selection(self):
        from playwright.async_api import async_playwright
        directory = self.enterContext(tempfile.TemporaryDirectory())
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.route('https://activesg.gov.sg/**', lambda r: r.fulfill(content_type='text/html', body='''
                  <h1>Tennis venues</h1><div id="venues"></div><script>
                  setTimeout(() => document.getElementById('venues').innerHTML =
                    '<a href="/facility-bookings/venues/kallang"><h3>Kallang Tennis Centre</h3><p>6 courts available</p></a>', 250);
                  </script>'''))
                await page.goto(ACTIVITIES)
                scanner = Scanner(directory)
                scanner.page = page
                selected = await scanner.find_link(page, 'Kallang Tennis Centre', kind='venue', timeout=1, poll_interval=0.01)
                self.assertEqual(selected, 'https://activesg.gov.sg/facility-bookings/venues/kallang')
            finally:
                await browser.close()

    async def test_missing_name_reports_candidates_without_selecting_other_court(self):
        from playwright.async_api import async_playwright
        from query_diagnostics import format_failure
        directory = self.enterContext(tempfile.TemporaryDirectory())
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.route('https://activesg.gov.sg/**', lambda r: r.fulfill(content_type='text/html', body='''
                    <h1>Tennis venues</h1><a href="/facility-bookings/venues/other"><h3>Kallang Tennis Hub</h3><p>courts</p></a>
                    <a href="/facility-bookings/venues/east">Our Tampines Hub</a>
                    <a href="https://example.com/login?token=private">Private unrelated link</a>'''))
                await page.goto(ACTIVITIES)
                scanner = Scanner(directory)
                scanner.page = page
                with self.assertRaises(ScanError) as caught:
                    await scanner.find_link(page, 'Kallang Tennis Centre', kind='venue', timeout=0.02, poll_interval=0.01)
                await scanner.save_failure(caught.exception)
                text = format_failure(scanner.last_failure())
                self.assertIn('Kallang Tennis Hub', text)
                self.assertNotIn('private', text)
                self.assertNotIn('https://', text)
                self.assertIn('场馆', text)
            finally:
                await browser.close()

    async def test_duplicate_names_with_different_destinations_are_rejected(self):
        from playwright.async_api import async_playwright
        directory = self.enterContext(tempfile.TemporaryDirectory())
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.route('https://activesg.gov.sg/**', lambda r: r.fulfill(content_type='text/html', body='''
                  <a href="/facility-bookings/venues/one">Kallang Tennis Centre</a>
                  <a href="/facility-bookings/venues/two">Kallang Tennis Centre</a>'''))
                await page.goto(ACTIVITIES)
                scanner = Scanner(directory)
                scanner.page = page
                with self.assertRaisesRegex(ScanError, '多个'):
                    await scanner.find_link(page, 'Kallang Tennis Centre', kind='venue', timeout=0.02)
            finally:
                await browser.close()


if __name__ == '__main__':
    unittest.main()
