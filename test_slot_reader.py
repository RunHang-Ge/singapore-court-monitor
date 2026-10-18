from dataclasses import replace
from datetime import date, datetime, timedelta
import tempfile
import unittest
from urllib.parse import urlparse

from playwright.async_api import async_playwright

from models import SG
from query_diagnostics import format_failure
from scanner import Scanner, ScanError, ACTIVITIES, assert_date
from test_monitor import config, slot


class SlotReaderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.driver = await async_playwright().start()
        self.browser = await self.driver.chromium.launch(headless=True)
        self.page = await self.browser.new_page()
        self.scanner = Scanner(self.directory)
        self.scanner.page = self.page
        self.c = config()
        self.stamp = int(datetime.fromisoformat(self.c.date_from + 'T12:00').replace(tzinfo=SG).timestamp() * 1000)

    async def asyncTearDown(self):
        await self.browser.close()
        await self.driver.stop()

    def control(self, offset=0, extra=''):
        return f'<label><input type="checkbox" value="{self.stamp + offset * 3600000}" {extra}>time</label>'

    async def load(self, html):
        await self.page.route('https://activesg.gov.sg/**', lambda route: route.fulfill(content_type='text/html', body=html))
        await self.page.goto(ACTIVITIES)

    async def read(self, timeout=0):
        return await self.scanner.read_slot_controls(self.page, self.c.date_from, timeout=timeout, poll_interval=0.01)

    async def test_plain_captions_and_unrelated_first_heading(self):
        await self.load('<h1>Kallang Tennis Centre</h1><main><h2>Choose a timeslot</h2>'
                        '<section><div><span>Instant</span> booking</div><div>' + self.control() + '</div></section>'
                        '<section><p>Ballot</p>' + self.control(1) + '</section></main>')
        controls = await self.read()
        self.assertEqual([c['bookingType'] for c in controls], ['instant', 'ballot'])

    async def test_sibling_captions_do_not_mix_ballot_into_instant(self):
        await self.load('<main><div>Instant booking</div><div>' + self.control() + '</div>'
                        '<div>Ballot</div><div>' + self.control(1) + '</div></main>')
        self.assertEqual([c['bookingType'] for c in await self.read()], ['instant', 'ballot'])

    async def test_aria_labelled_group_and_hidden_caption(self):
        await self.load('<div id="group-caption" hidden>Instant booking</div>'
                        '<section aria-labelledby="group-caption">' + self.control() + '</section>')
        self.assertEqual((await self.read())[0]['bookingType'], 'instant')

    async def test_group_caption_loaded_after_controls_is_waited_for(self):
        await self.load('<section><div id="caption">Loading...</div>' + self.control() +
                        '<script>setTimeout(() => document.getElementById("caption").textContent="Instant booking", 100)</script></section>')
        self.assertEqual((await self.read(timeout=1))[0]['bookingType'], 'instant')

    async def test_unknown_enabled_control_prevents_partial_result(self):
        await self.load('<main><section><div>Instant booking</div>' + self.control() + '</section>'
                        '<section><div>Other group</div>' + self.control(1) + '</section></main>')
        with self.assertRaises(ScanError) as caught:
            await self.read()
        await self.scanner.save_failure(caught.exception)
        saved = self.scanner.last_failure()
        self.assertEqual(saved['slots']['instant'], 1)
        self.assertEqual(saved['slots']['enabled_unknown'], 1)
        reply = format_failure(saved)
        self.assertIn('日期与可选时间', reply)
        self.assertIn(self.c.date_from, reply)
        self.assertNotIn(str(self.stamp), reply)
        self.assertNotIn('https://', reply)

    async def test_loading_instant_grid_does_not_return_ballot_only_early(self):
        await self.load('<main><section><div>Instant booking</div><div id="grid">Loading...</div></section>'
                        '<section><div>Ballot</div>' + self.control(1) + '</section></main>')
        await self.page.evaluate("html => setTimeout(() => document.getElementById('grid').innerHTML=html, 100)", self.control())
        controls = await self.read(timeout=1)
        self.assertEqual([c['bookingType'] for c in controls], ['instant', 'ballot'])

    async def test_navigation_caption_is_not_evidence_of_instant_group(self):
        await self.load('<nav><div>Instant booking</div></nav><main><div>' + self.control() + '</div></main>')
        with self.assertRaises(ScanError):
            await self.read()

    async def test_confirmed_ballot_only_page_has_no_instant_controls(self):
        await self.load('<section><h2>Ballot</h2>' + self.control() + '</section>')
        self.assertEqual([c['bookingType'] for c in await self.read()], ['ballot'])

    async def test_stale_previous_day_controls_are_not_accepted(self):
        yesterday = self.stamp - 86400000
        await self.load(f'<section><h2>Instant booking</h2><label><input type="checkbox" value="{yesterday}">old time</label></section>')
        with self.assertRaises(ScanError) as caught:
            await self.read()
        self.assertEqual(caught.exception.diagnostic['slots']['stale_instant'], 1)

    async def test_selected_date_without_query_parameter_is_verified(self):
        label = date.fromisoformat(self.c.date_from).strftime('%a, %d %b')
        self.assertTrue(assert_date(self.c.date_from, ACTIVITIES, [{'label': label, 'selected': True}]))
        self.assertFalse(assert_date(self.c.date_from, ACTIVITIES, [{'label': 'wrong date', 'selected': True}]))

    async def test_click_navigation_date_switch_and_complete_hour_filter(self):
        last = (date.fromisoformat(self.c.date_from) + timedelta(days=1)).isoformat()
        c = replace(self.c, date_to=last)
        stamps = [self.stamp, self.stamp + 86400000]
        labels = [date.fromisoformat(day).strftime('%a, %d %b') for day in (c.date_from, last)]
        buttons = ''.join(f'<button aria-label="View timeslots for {label}" onclick="selectDate(this,{stamp})">{label}</button>'
                          for label, stamp in zip(labels, stamps))
        await self.page.unroute('https://activesg.gov.sg/**')
        async def fixture(route):
            path = urlparse(route.request.url).path
            if path == '/facility-bookings/activities':
                html = '<a href="/facility-bookings/activities/tennis" onclick="sessionStorage.setItem(\'activityClicked\',\'yes\')">Tennis</a>'
            elif path == '/facility-bookings/activities/tennis':
                html = '<a href="/facility-bookings/venues/kallang" onclick="sessionStorage.setItem(\'venueClicked\',\'yes\')">Kallang Tennis CentreLoading...</a>'
            else:
                html = '<h1>Kallang Tennis Centre</h1><p>Each slot is 1 hour long.</p>' + buttons + '''
                  <main><h2>Choose your slots</h2><section><div>Instant booking</div><div id="instant"></div></section>
                  <section><div>Ballot</div><div id="ballot"></div></section></main>
                  <script>
                    function selectDate(button, stamp) {
                      if (sessionStorage.getItem('activityClicked') !== 'yes' || sessionStorage.getItem('venueClicked') !== 'yes') return;
                      document.querySelectorAll('button').forEach(n => n.dataset.selected = 'false');
                      button.dataset.selected = 'true';
                      setTimeout(() => {
                        document.getElementById('instant').innerHTML = '<label><input type="checkbox" value="' + stamp + '">12:00</label>' +
                          '<label><input type="checkbox" disabled value="' + (stamp+3600000) + '">13:00</label>';
                        document.getElementById('ballot').innerHTML = '<label><input type="checkbox" value="' + (stamp+3600000) + '">13:00</label>';
                      }, 100);
                    }
                  </script>'''
            await route.fulfill(content_type='text/html', body=html)
        await self.page.route('https://activesg.gov.sg/**', fixture)
        result = await self.scanner.scan(c)
        self.assertEqual(result['matches'], [slot(), {**slot(13), 'booking_type': 'ballot'},
            {**slot(), 'date': last, 'key': last + '|12:00'},
            {**slot(13), 'date': last, 'key': last + '|13:00', 'booking_type': 'ballot'}])
        self.assertTrue(all(row['status'] == 'checked' for row in result['results']))
        self.assertEqual(await self.page.evaluate("sessionStorage.getItem('venueClicked')"), 'yes')

    async def test_disabled_and_absent_date_buttons_are_skipped(self):
        label = date.fromisoformat(self.c.date_from).strftime('%a, %d %b')
        await self.load(f'<button aria-label="View timeslots for {label}" disabled>{label}</button>')
        self.assertFalse(await self.scanner.select_day(self.page, self.c.date_from, timeout=0))
        missing = (date.fromisoformat(self.c.date_from) + timedelta(days=1)).isoformat()
        self.assertFalse(await self.scanner.select_day(self.page, missing, timeout=0))


if __name__ == '__main__':
    unittest.main()
