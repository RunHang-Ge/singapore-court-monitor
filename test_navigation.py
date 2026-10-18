import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock

from playwright.async_api import async_playwright, TimeoutError as BrowserTimeout

from query_diagnostics import format_failure
from scanner import Scanner, ScanError, ACTIVITIES
from test_monitor import config


class NavigationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.driver = await async_playwright().start()
        self.browser = await self.driver.chromium.launch(headless=True)
        self.page = await self.browser.new_page()
        self.scanner = Scanner(self.directory)
        self.scanner.page = self.page
        self.destination = 'https://activesg.gov.sg/facility-bookings/venues/kallang'

    async def asyncTearDown(self):
        await self.browser.close()
        await self.driver.stop()

    async def load(self, html):
        await self.page.route('https://activesg.gov.sg/**', lambda route: route.fulfill(content_type='text/html', body=html))
        await self.page.goto(ACTIVITIES)

    async def test_route_and_visible_content_do_not_wait_for_slow_image(self):
        gate = asyncio.Event()
        async def fixture(route):
            if route.request.resource_type == 'image':
                await gate.wait()
                await route.fulfill(status=204)
            elif route.request.url == ACTIVITIES:
                await route.fulfill(content_type='text/html', body='<a href="/facility-bookings/venues/kallang">Kallang Tennis Centre</a>')
            else:
                await route.fulfill(content_type='text/html', body='<h1>Kallang Tennis Centre</h1><button aria-label="View timeslots for Mon, 12 Oct">date</button><img src="/facility-bookings/slow-image">')
        await self.page.route('https://activesg.gov.sg/**', fixture)
        try:
            await self.page.goto(ACTIVITIES)
            text = await asyncio.wait_for(self.scanner.click_link(self.page, self.destination, timeout=0.8), timeout=1.5)
            self.assertIn('Kallang Tennis Centre', text)
            self.assertEqual(await self.page.locator('[aria-label^="View timeslots for"]').count(), 1)
            self.assertNotEqual(await self.page.evaluate('document.readyState'), 'complete')
        finally:
            gate.set()
            await self.page.wait_for_load_state('load', timeout=2000)

    async def test_initial_activities_page_does_not_wait_for_deferred_script(self):
        gate = asyncio.Event()
        async def fixture(route):
            if route.request.resource_type == 'script':
                await gate.wait()
                await route.fulfill(content_type='application/javascript', body='')
            else:
                await route.fulfill(content_type='text/html', body='<h1>Activities</h1><a href="/facility-bookings/activities/tennis">Tennis</a><script defer src="/facility-bookings/slow-script"></script>')
        await self.page.route('https://activesg.gov.sg/**', fixture)
        try:
            text = await asyncio.wait_for(self.scanner.goto(self.page, ACTIVITIES), timeout=1.5)
            self.assertIn('Tennis', text)
            self.assertEqual(await self.scanner.find_link(self.page, 'Tennis', timeout=1, poll_interval=0.02),
                             'https://activesg.gov.sg/facility-bookings/activities/tennis')
            self.assertNotEqual(await self.page.evaluate('document.readyState'), 'complete')
        finally:
            gate.set()
            await self.page.wait_for_load_state('load', timeout=2000)

    async def test_hidden_duplicate_link_is_not_clicked(self):
        async def fixture(route):
            if route.request.url == ACTIVITIES:
                html = '<a hidden href="/facility-bookings/venues/kallang">Kallang Tennis Centre</a>' + \
                       '<a href="/facility-bookings/venues/kallang" onclick="sessionStorage.setItem(\'clicked\',\'yes\')">Kallang Tennis Centre</a>'
            else:
                html = '<h1>Kallang Tennis Centre</h1>'
            await route.fulfill(content_type='text/html', body=html)
        await self.page.route('https://activesg.gov.sg/**', fixture)
        await self.page.goto(ACTIVITIES)
        await self.scanner.click_link(self.page, self.destination, timeout=0.8)
        self.assertEqual(await self.page.evaluate("sessionStorage.getItem('clicked')"), 'yes')

    async def test_canonical_trailing_slash_does_not_cause_route_timeout(self):
        # A fulfilled HTTP redirect may escape Playwright's request route.
        # Keep this canonical-path case entirely within the simulated page.
        await self.load("""<a href="/facility-bookings/venues/kallang" onclick="event.preventDefault();
          history.pushState({},'',this.href+'/');document.body.innerHTML='<h1>Kallang Tennis Centre</h1>'">
          Kallang Tennis Centre</a>""")
        await self.scanner.click_link(self.page, self.destination, timeout=0.8)
        self.assertEqual(self.page.url, self.destination + '/')

    async def test_client_navigation_without_document_load_is_supported(self):
        await self.load('<a href="/facility-bookings/venues/kallang" onclick="event.preventDefault();history.pushState({},\'\',this.href);document.body.innerHTML=\'<h1>Kallang Tennis Centre</h1>\'">Kallang Tennis Centre</a>')
        self.assertIn('Kallang Tennis Centre', await self.scanner.click_link(self.page, self.destination, timeout=0.8))
        self.assertEqual(self.page.url, self.destination)

    async def test_blocking_overlay_reports_button_timeout_and_stage(self):
        await self.load('<h1>ActiveSG</h1><a href="/facility-bookings/venues/kallang">Kallang Tennis Centre</a>'
                        '<div role="dialog" style="position:fixed;inset:0;background:white;z-index:99">Notice</div>')
        async def blocked(_):
            self.scanner.mark_step('进入运动项目的场馆列表', target='Tennis')
            await self.scanner.click_link(self.page, self.destination, timeout=0.35)
        self.scanner._scan = blocked
        with self.assertRaises(ScanError) as caught:
            await self.scanner.scan(config())
        saved = self.scanner.last_failure()
        self.assertIn('点击Tennis', str(caught.exception))
        self.assertEqual(saved['step']['stage'], '进入运动项目的场馆列表')
        self.assertFalse(saved['step']['clicked'])
        self.assertEqual(saved['step']['timeout_reason'], '其他页面元素挡住按钮')
        self.assertEqual(saved['step']['page']['dialogs'], 1)
        self.assertEqual(saved['step']['page']['date_buttons'], 0)
        self.assertTrue(saved['screenshot_sha256'])
        self.assertEqual(self.scanner.progress, {})

    async def test_click_without_navigation_reports_separate_waiting_step(self):
        await self.load('<h1>ActiveSG</h1><a href="/facility-bookings/venues/kallang" onclick="event.preventDefault()">Kallang Tennis Centre</a>')
        async def stalled(_):
            self.scanner.mark_step('进入运动项目的场馆列表', target='Tennis')
            await self.scanner.click_link(self.page, self.destination, timeout=0.25)
        self.scanner._scan = stalled
        with self.assertRaises(ScanError):
            await self.scanner.scan(config())
        saved = self.scanner.last_failure()
        self.assertTrue(saved['step']['clicked'])
        self.assertEqual(saved['step']['operation'], '等待点击后的页面切换')
        self.assertEqual(saved['url'], ACTIVITIES)
        reply = format_failure(saved)
        self.assertIn('按钮点击：已完成', reply)
        self.assertIn('等待点击后的页面切换', reply)
        self.assertNotIn('https://', reply)

    async def test_raw_playwright_error_credentials_are_never_saved(self):
        await self.load('<h1>ActiveSG</h1>')
        self.scanner._scan = AsyncMock(side_effect=BrowserTimeout('Timeout waiting for https://browser.vendor/?token=private-test-credential'))
        with self.assertRaises(ScanError):
            await self.scanner.scan(config())
        raw = (Path(self.directory) / 'last-query-failure.json').read_text(encoding='utf-8')
        self.assertNotIn('private-test-credential', raw)
        self.assertNotIn('browser.vendor', raw)
        self.assertNotIn('private-test-credential', format_failure(json.loads(raw)))


if __name__ == '__main__':
    unittest.main()
