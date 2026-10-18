import json
from pathlib import Path
import tempfile
import unittest

from playwright.async_api import async_playwright

from query_diagnostics import format_failure
from scanner import Scanner, ScanError, ACTIVITIES


class ScrollingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.driver = await async_playwright().start()
        self.browser = await self.driver.chromium.launch(headless=True)
        self.page = await self.browser.new_page(viewport={'width': 1280, 'height': 900})
        self.scanner = Scanner(self.directory)
        self.scanner.page = self.page
        self.destination = 'https://activesg.gov.sg/facility-bookings/activities/tennis'

    async def asyncTearDown(self):
        await self.browser.close()
        await self.driver.stop()

    async def load(self, html):
        async def fixture(route):
            body = html if route.request.url == ACTIVITIES else '<h1>Tennis venues</h1>'
            await route.fulfill(content_type='text/html', body=body)
        await self.page.route('https://activesg.gov.sg/**', fixture)
        await self.page.goto(ACTIVITIES)

    async def find_and_click(self):
        self.scanner.mark_step('查找运动项目', target='Tennis')
        link = await self.scanner.find_link(self.page, 'Tennis', timeout=1.5, poll_interval=0.02)
        self.assertEqual(link, self.destination)
        self.scanner.mark_step('进入运动项目的场馆列表', target='Tennis')
        await self.scanner.click_link(self.page, link, timeout=1)
        self.assertEqual(self.page.url, self.destination)

    async def test_existing_offscreen_card_is_scrolled_into_view_before_click(self):
        await self.load('''<h1>Activities</h1><div style="height:2400px">Other sports</div>
          <a href="/facility-bookings/activities/tennis" onclick="sessionStorage.setItem('inViewport',String(this.getBoundingClientRect().top < innerHeight))">Tennis</a>''')
        box = await self.page.locator('a').bounding_box()
        self.assertGreater(box['y'], 900)
        await self.find_and_click()
        self.assertEqual(await self.page.evaluate("sessionStorage.getItem('inViewport')"), 'true')
        self.assertTrue(self.scanner.progress['target_scrolled'])
        self.assertTrue(self.scanner.progress['target_in_view'])
        self.assertTrue(self.scanner.progress['clicked'])

    async def test_scroll_reveals_card_missing_from_initial_dom(self):
        await self.load('''<h1>Activities</h1><a href="/facility-bookings/activities/badminton">Badminton</a>
          <div style="height:2000px">More sports below</div><div id="cards"></div>
          <script>addEventListener('scroll', () => {
            if (scrollY > 300 && !document.querySelector('#cards a')) {
              document.getElementById('cards').innerHTML = '<a href="/facility-bookings/activities/tennis">Tennis</a>';
              sessionStorage.setItem('lazyLoaded', 'yes');
            }
          });</script>''')
        self.assertEqual(await self.page.locator('a[href$="/tennis"]').count(), 0)
        await self.find_and_click()
        self.assertEqual(await self.page.evaluate("sessionStorage.getItem('lazyLoaded')"), 'yes')

    async def test_nested_list_scroll_loads_and_clicks_tennis(self):
        await self.load('''<h1>Activities</h1><main id="list" style="height:320px;width:800px;overflow-y:auto">
          <section style="min-height:2000px"><a href="/facility-bookings/activities/badminton">Badminton</a>
          <div style="height:1800px"></div><div id="cards"></div></section></main>
          <script>document.getElementById('list').addEventListener('scroll', function () {
            if (this.scrollTop > 300 && !document.querySelector('#cards a')) {
              document.getElementById('cards').innerHTML = '<a href="/facility-bookings/activities/tennis">Tennis</a>';
              sessionStorage.setItem('nestedLoaded', 'yes');
            }
          });</script>''')
        self.assertEqual(await self.page.evaluate('scrollY'), 0)
        await self.find_and_click()
        self.assertEqual(await self.page.evaluate("sessionStorage.getItem('nestedLoaded')"), 'yes')

    async def test_hidden_responsive_card_does_not_create_false_ambiguity(self):
        await self.load('''<h1>Activities</h1>
          <a hidden href="/facility-bookings/activities/old-tennis">Tennis</a>
          <a href="/facility-bookings/activities/tennis">Tennis</a>''')
        await self.find_and_click()

    async def test_hidden_relative_link_does_not_hide_visible_absolute_card(self):
        await self.load('''<h1>Activities</h1>
          <a hidden href="/facility-bookings/activities/tennis">Tennis</a>
          <div style="height:2000px"></div>
          <a href="https://activesg.gov.sg/facility-bookings/activities/tennis">Tennis</a>''')
        await self.find_and_click()
        self.assertTrue(self.scanner.progress['target_in_view'])

    async def test_display_contents_link_uses_its_visible_card_for_scrolling(self):
        await self.load('''<h1>Activities</h1><div style="height:2000px"></div>
          <a style="display:contents" href="/facility-bookings/activities/tennis"><div style="padding:20px">Tennis</div></a>''')
        box = await self.page.locator('a').bounding_box()
        self.assertTrue(box is None or box['width'] == 0 or box['height'] == 0)
        await self.find_and_click()
        self.assertTrue(self.scanner.progress['target_in_view'])

    async def test_failed_search_saves_scrolling_evidence_and_candidate_names(self):
        await self.load('<h1>Activities</h1><a href="/facility-bookings/activities/badminton">Badminton</a><div style="height:2000px"></div>')
        self.scanner.mark_step('查找运动项目', target='Tennis')
        with self.assertRaises(ScanError) as caught:
            await self.scanner.find_link(self.page, 'Tennis', timeout=0.3, poll_interval=0.02)
        await self.scanner.save_failure(caught.exception)
        saved = self.scanner.last_failure()
        self.assertGreater(saved['step']['search_scroll_steps'], 0)
        self.assertGreater(saved['step']['search_scroll_distance'], 0)
        self.assertIn('Badminton', saved['lookup']['candidate_names'])
        self.assertIn('滚动查找', format_failure(saved))
        self.assertNotIn('https://', format_failure(saved))
        self.assertEqual(json.loads((Path(self.directory) / 'last-query-failure.json').read_text(encoding='utf-8'))['step']['stage'], '查找运动项目')

    async def test_visible_target_still_requires_scrolling_to_the_bottom(self):
        await self.load('''<h1>Activities</h1>
          <a href="/facility-bookings/activities/tennis">Tennis</a>
          <div style="height:6500px">Other sports below</div>''')
        self.assertLess((await self.page.locator('a').bounding_box())['y'], 900)
        link = await self.scanner.find_link(self.page, 'Tennis', timeout=2, poll_interval=0.02)
        self.assertEqual(link, self.destination)
        self.assertTrue(await self.page.evaluate('scrollY + innerHeight >= document.scrollingElement.scrollHeight - 4'))
        self.assertGreater(self.scanner.progress['search_scroll_steps'], 5)
        self.assertTrue(self.scanner.progress['search_bottom_confirmed'])

    async def test_new_content_at_bottom_is_scrolled_until_the_final_bottom(self):
        await self.load('''<h1>Activities</h1>
          <a href="/facility-bookings/activities/tennis">Tennis</a>
          <div style="height:1400px">First batch</div><script>
          window.batches = 0; window.pending = false;
          addEventListener('scroll', () => {
            if (scrollY + innerHeight >= document.scrollingElement.scrollHeight - 4 &&
                batches < 2 && !pending) {
              pending = true;
              setTimeout(() => {
                const next = document.createElement('div');
                next.style.height = '1400px'; next.textContent = 'Next sports';
                document.body.append(next); batches++; pending = false;
              }, 200);
            }
          });</script>''')
        self.assertEqual(await self.scanner.find_link(self.page, 'Tennis', timeout=3, poll_interval=0.02), self.destination)
        self.assertEqual(await self.page.evaluate('batches'), 2)
        self.assertTrue(await self.page.evaluate('scrollY + innerHeight >= document.scrollingElement.scrollHeight - 4'))
        self.assertTrue(self.scanner.progress['search_bottom_confirmed'])

    async def test_nested_list_reaches_bottom_even_if_target_is_initially_visible(self):
        await self.load('''<h1>Activities</h1>
          <main id="list" style="height:320px;width:800px;overflow-y:auto">
          <a href="/facility-bookings/activities/tennis">Tennis</a>
          <div style="height:6500px">Other sports below</div></main>''')
        self.assertEqual(await self.scanner.find_link(self.page, 'Tennis', timeout=4, poll_interval=0.02), self.destination)
        self.assertTrue(await self.page.locator('#list').evaluate('n => n.scrollTop + n.clientHeight >= n.scrollHeight - 4'))
        self.assertEqual(await self.page.evaluate('scrollY'), 0)
        self.assertTrue(self.scanner.progress['search_bottom_confirmed'])

    async def test_never_ending_list_times_out_without_accepting_early_target(self):
        await self.load('''<h1>Activities</h1>
          <a href="/facility-bookings/activities/tennis">Tennis</a>
          <div style="height:1400px"></div><script>
          addEventListener('scroll', () => {
            if (scrollY + innerHeight >= document.scrollingElement.scrollHeight - 4) {
              const next = document.createElement('div'); next.style.height = '1400px';
              document.body.append(next);
            }
          });</script>''')
        with self.assertRaisesRegex(ScanError, '尚未确认列表到底') as caught:
            await self.scanner.find_link(self.page, 'Tennis', timeout=0.45, poll_interval=0.02)
        await self.scanner.save_failure(caught.exception)
        saved = self.scanner.last_failure()
        self.assertFalse(saved['step']['search_bottom_confirmed'])
        self.assertIn('列表是否已滚动到底：尚未确认', format_failure(saved))

    async def test_pause_during_scrolling_stops_before_reaching_bottom(self):
        await self.load('''<h1>Activities</h1>
          <a href="/facility-bookings/activities/tennis">Tennis</a>
          <div style="height:6500px"></div>''')
        self.scanner.is_current = lambda: self.scanner.progress.get('search_scroll_steps', 0) < 2
        with self.assertRaisesRegex(ScanError, '已取消'):
            await self.scanner.find_link(self.page, 'Tennis', timeout=2, poll_interval=0.02)
        self.assertEqual(self.scanner.progress['search_scroll_steps'], 2)
        self.assertFalse(await self.page.evaluate('scrollY + innerHeight >= document.scrollingElement.scrollHeight - 4'))


    async def test_cancelled_query_does_not_scroll_or_click(self):
        await self.load('<h1>Activities</h1><div style="height:2000px"></div>')
        self.scanner.is_current = lambda: False
        with self.assertRaises(ScanError):
            await self.scanner.scroll_search(self.page)
        self.assertEqual(await self.page.evaluate('scrollY'), 0)


if __name__ == '__main__':
    unittest.main()
