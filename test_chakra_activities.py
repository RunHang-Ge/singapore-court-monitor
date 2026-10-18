from datetime import date, datetime
import json
import re
import tempfile
import unittest
from urllib.parse import urlparse

from playwright.async_api import async_playwright

from activity_controls import ActivityButton
from models import SG
from query_diagnostics import format_failure
from scanner import Scanner, ScanError, ACTIVITIES
from test_monitor import config, slot


# Public DOM copied by the user from the real expanded ActiveSG Tennis card.
TENNIS_CARD = '''<div class="chakra-card css-v8jdfd"><div class="chakra-card__body css-10z5y9g"><div class="chakra-stack css-axuxix"><p class="chakra-text css-5s5n1x">Tennis</p><svg stroke="currentColor" fill="currentColor" stroke-width="0" viewBox="0 0 24 24" height="1em" width="1em" xmlns="http://www.w3.org/2000/svg"><path d="m6.293 13.293 1.414 1.414L12 10.414l4.293 4.293 1.414-1.414L12 7.586z"></path></svg></div><div class="chakra-collapse" style="overflow: hidden; display: block; opacity: 1; height: auto;"><div class="chakra-stack css-1qprny0"><hr aria-orientation="horizontal" class="chakra-divider css-y1mfdc"><a class="css-3axv7w" href="/facility-bookings/activities/Yze2WwODgEaBXVtBQ0ze9/venues"><p class="chakra-text css-5s5n1x">Mini Tennis</p></a><hr aria-orientation="horizontal" class="chakra-divider css-y1mfdc"><a class="css-3axv7w" href="/facility-bookings/activities/B0KovYOcQun1mA4VowDq0/venues"><p class="chakra-text css-5s5n1x">Tennis</p></a><hr aria-orientation="horizontal" class="chakra-divider css-y1mfdc"><a class="css-3axv7w" href="/facility-bookings/activities/vd0UBIMHcr8NnuIwN9lbz/venues"><p class="chakra-text css-5s5n1x">Tennis Wall</p></a></div></div></div></div>'''
TENNIS_PATH = '/facility-bookings/activities/B0KovYOcQun1mA4VowDq0/venues'


class ChakraActivityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.driver = await async_playwright().start()
        self.browser = await self.driver.chromium.launch(headless=True)
        self.page = await self.browser.new_page(viewport={'width': 1280, 'height': 900})
        self.scanner = Scanner(self.directory)
        self.scanner.page = self.page
        self.destination = 'https://activesg.gov.sg' + TENNIS_PATH
        self.requested = []

    async def asyncTearDown(self):
        await self.browser.close()
        await self.driver.stop()

    async def load(self, card=TENNIS_CARD, opened=False, outside=False, private_field=False):
        if not opened:
            # A collapsed Chakra panel can retain laid-out links inside a
            # zero-height clipping region; their own boxes are not proof of visibility.
            card = card.replace('opacity: 1; height: auto;', 'opacity: 0; height: 0px;')
        c = config()
        stamp = int(datetime.fromisoformat(c.date_from + 'T12:00').replace(tzinfo=SG).timestamp() * 1000)
        label = date.fromisoformat(c.date_from).strftime('%a, %d %b')
        html = '''<h1>Book a facility</h1><a href="/facility-bookings/activities/table-tennis/venues">Table Tennis</a>
          <style>.chakra-card{border:1px solid #ddd}.chakra-card__body>.chakra-stack{display:flex;justify-content:space-between;align-items:center;padding:12px;cursor:pointer}.chakra-card__body p{margin:0}.chakra-collapse a{display:block;padding:8px}</style>
          <div style="height:1400px">Other sports</div>''' + card
        if outside:
            html += '<a id="outside" hidden href="/facility-bookings/activities/incorrect/venues">Tennis</a>'
        if private_field:
            html += '<input type="password" value="not-for-diagnostics">'
        html += '''<script>document.querySelectorAll('.chakra-card__body > .chakra-stack').forEach(row => {
          row.addEventListener('click', () => {
            const panel = row.nextElementSibling;
            panel.style.display = 'block'; panel.style.opacity = '1'; panel.style.height = 'auto';
            sessionStorage.setItem('openedSport', row.querySelector('p').textContent);
            const outside = document.getElementById('outside'); if (outside) outside.hidden = false;
          });
        });</script>'''
        async def fixture(route):
            path = urlparse(route.request.url).path
            self.requested.append(path)
            if path == '/facility-bookings/activities':
                body = html
            elif path == TENNIS_PATH:
                body = '<h1>Tennis venues</h1><a href="/facility-bookings/venues/kallang">Kallang Tennis CentreLoading...</a>'
            elif path == '/facility-bookings/venues/kallang':
                body = (f'<h1>Kallang Tennis Centre</h1><p>Each slot is 1 hour long.</p>'
                        f'<button aria-label="View timeslots for {label}" data-selected="true">date</button>'
                        f'<section><h2>Instant booking</h2><label><input type="checkbox" value="{stamp}">12:00</label></section>')
            else:
                body = '<h1>Incorrect activity</h1>'
            await route.fulfill(content_type='text/html', body=body)
        await self.page.route('https://activesg.gov.sg/**', fixture)
        await self.page.goto(ACTIVITIES)

    async def find(self):
        self.scanner.mark_step('查找运动项目', target='Tennis')
        return await self.scanner.find_link(self.page, 'Tennis', timeout=3, poll_interval=0.02)

    async def test_real_zero_height_collapse_is_expanded_before_selecting_tennis(self):
        await self.load()
        box = await self.page.locator('a[href="' + TENNIS_PATH + '"]').bounding_box()
        self.assertGreater(box['height'], 0)
        target = await self.find()
        self.assertIsInstance(target, ActivityButton)
        self.assertTrue(target.chakra_card)
        self.assertTrue(self.scanner.progress['search_bottom_confirmed'])
        await self.scanner.click_link(self.page, target, timeout=3)
        self.assertEqual(self.page.url, self.destination)
        self.assertEqual(await self.page.evaluate("sessionStorage.getItem('openedSport')"), 'Tennis')
        self.assertEqual(self.scanner.progress['activity_options'], ['Mini Tennis', 'Tennis', 'Tennis Wall'])

    async def test_real_expanded_card_uses_existing_opaque_tennis_link(self):
        await self.load(opened=True)
        target = await self.find()
        self.assertEqual(target, self.destination)
        await self.scanner.click_link(self.page, target, timeout=3)
        self.assertEqual(self.page.url, self.destination)
        self.assertIsNone(await self.page.evaluate("sessionStorage.getItem('openedSport')"))

    async def test_already_open_card_does_not_click_same_href_in_earlier_closed_card(self):
        closed_mini = TENNIS_CARD.replace('>Tennis</p>', '>Mini Tennis</p>', 1).replace(
            'opacity: 1; height: auto;', 'opacity: 0; height: 0px;')
        await self.load(card=closed_mini + TENNIS_CARD, opened=True)
        self.assertEqual(await self.find(), self.destination)
        await self.scanner.click_link(self.page, self.destination, timeout=3)
        self.assertEqual(self.page.url, self.destination)
        self.assertIsNone(await self.page.evaluate("sessionStorage.getItem('openedSport')"))

    async def test_generated_css_hash_changes_do_not_break_card_selection(self):
        card = re.sub(r'css-[a-z0-9]+', 'css-new-release', TENNIS_CARD)
        await self.load(card=card)
        await self.scanner.click_link(self.page, await self.find(), timeout=3)
        self.assertEqual(self.page.url, self.destination)

    async def test_other_similarly_named_disclosure_cards_are_not_opened(self):
        mini = TENNIS_CARD.replace('>Tennis</p>', '>Mini Tennis</p>', 1)
        wall = TENNIS_CARD.replace('>Tennis</p>', '>Tennis Wall</p>', 1)
        await self.load(card=mini + TENNIS_CARD + wall)
        await self.scanner.click_link(self.page, await self.find(), timeout=3)
        self.assertEqual(self.page.url, self.destination)
        self.assertEqual(await self.page.evaluate("sessionStorage.getItem('openedSport')"), 'Tennis')

    async def test_revealed_matching_link_outside_card_is_not_selected(self):
        await self.load(outside=True)
        await self.scanner.click_link(self.page, await self.find(), timeout=3)
        self.assertEqual(self.page.url, self.destination)
        self.assertNotIn('/facility-bookings/activities/incorrect/venues', self.requested)

    async def test_full_query_with_real_card_reaches_kallang_and_reads_slot(self):
        await self.load()
        result = await self.scanner.scan(config())
        self.assertEqual(result['matches'], [slot()])
        self.assertEqual(result['results'], [{'date': config().date_from, 'status': 'checked'}])
        self.assertIn(TENNIS_PATH, self.requested)
        self.assertEqual(self.page.url, 'https://activesg.gov.sg/facility-bookings/venues/kallang')

    async def test_unrecognized_target_shape_records_bounded_dom_evidence_without_fields(self):
        card = TENNIS_CARD.replace('class="chakra-stack css-axuxix"', 'class="custom-stack css-axuxix"')
        await self.load(card=card, private_field=True)
        with self.assertRaises(ScanError) as caught:
            await self.scanner.find_link(self.page, 'Tennis', timeout=0.35, poll_interval=0.02)
        await self.scanner.save_failure(caught.exception)
        saved = self.scanner.last_failure()
        self.assertTrue(saved['lookup']['target_elements'])
        text = format_failure(saved)
        self.assertIn('目标文字所在元素：', text)
        self.assertIn('div.custom-stack', text)
        self.assertNotIn('not-for-diagnostics', json.dumps(saved))
        self.assertNotIn('https://', text)


if __name__ == '__main__':
    unittest.main()
