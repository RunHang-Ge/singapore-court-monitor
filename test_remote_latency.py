import asyncio
import tempfile
import time
import unittest

from playwright.async_api import async_playwright

from scanner import Scanner, ScanError, SecurityCheckPending, ACTIVITIES


class RemoteLatencyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.driver = await async_playwright().start()
        self.browser = await self.driver.chromium.launch(headless=True)
        self.page = await self.browser.new_page(viewport={'width': 1280, 'height': 900})
        self.scanner = Scanner(self.directory)
        self.scanner.page = self.page

    async def asyncTearDown(self):
        await self.browser.close()
        await self.driver.stop()

    async def load(self, html):
        await self.page.route('https://activesg.gov.sg/**', lambda route: route.fulfill(content_type='text/html', body=html))
        await self.page.goto(ACTIVITIES)

    async def test_page_classification_uses_one_remote_snapshot_with_consistent_document(self):
        await self.load('<title>ActiveSG</title><h1>Book a facility</h1><p>Tennis</p>')
        calls = []
        page = self.page
        class OneReadPage:
            async def evaluate(self, script):
                calls.append('snapshot')
                await asyncio.sleep(0.05)
                return await page.evaluate(script)
        diagnostic, text = await self.scanner.inspect_page(OneReadPage())
        self.assertEqual(calls, ['snapshot'])
        self.assertEqual(diagnostic['title'], 'ActiveSG')
        self.assertEqual(diagnostic['category'], 'page_loaded')
        self.assertIn('Tennis', text)

    async def test_atomic_snapshot_preserves_challenge_markers(self):
        await self.load('<title>Just a moment...</title><p>Performing security verification</p><script>window._cf_chl_opt={};</script>')
        with self.assertRaises(SecurityCheckPending) as caught:
            await self.scanner.guard(self.page)
        self.assertEqual(caught.exception.diagnostic['category'], 'cloudflare_challenge')

    async def test_remote_search_can_finish_after_old_twelve_second_budget(self):
        await self.load('<title>ActiveSG</title><h1>Activities</h1><a href="/facility-bookings/activities/tennis">Tennis</a><div style="height:1800px"></div>')
        self.scanner.remote_deadline = time.monotonic() + 100
        original_scroll = self.scanner.scroll_search
        delay_once = True
        async def delayed_scroll(page, reset=False):
            nonlocal delay_once
            if not reset and delay_once:
                delay_once = False
                # Simulate a remote call consuming the old search budget. No
                # clock changes are made; cancellation and deadlines stay real.
                await asyncio.sleep(12.1)
            return await original_scroll(page, reset=reset)
        self.scanner.scroll_search = delayed_scroll
        selected = await self.scanner.find_link(self.page, 'Tennis', poll_interval=0.02)
        self.assertEqual(selected, 'https://activesg.gov.sg/facility-bookings/activities/tennis')
        self.assertEqual(self.scanner.progress['timeout_seconds'], 30)
        self.assertTrue(self.scanner.progress['search_bottom_confirmed'])

    async def test_remote_remaining_session_shortens_search_budget(self):
        await self.load('<title>ActiveSG</title><h1>Activities</h1><a href="/facility-bookings/activities/tennis">Tennis</a>')
        self.scanner.remote_deadline = time.monotonic() + 15.4
        selected = await self.scanner.find_link(self.page, 'Tennis', poll_interval=0.01)
        self.assertEqual(selected, 'https://activesg.gov.sg/facility-bookings/activities/tennis')
        self.assertGreater(self.scanner.progress['timeout_seconds'], 0)
        self.assertLessEqual(self.scanner.progress['timeout_seconds'], 0.4)
        self.assertTrue(self.scanner.progress['search_bottom_confirmed'])

    async def test_explicit_short_budget_still_rejects_partial_scroll(self):
        await self.load('<title>ActiveSG</title><h1>Activities</h1><a href="/facility-bookings/activities/tennis">Tennis</a><div style="height:10000px"></div>')
        self.scanner.remote_deadline = time.monotonic() + 100
        with self.assertRaisesRegex(ScanError, '尚未确认列表到底'):
            await self.scanner.find_link(self.page, 'Tennis', timeout=0.15, poll_interval=0.02)
        self.assertFalse(self.scanner.progress['search_bottom_confirmed'])


if __name__ == '__main__':
    unittest.main()
