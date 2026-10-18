from datetime import date, datetime
import tempfile
import unittest
from urllib.parse import urlparse

from playwright.async_api import async_playwright

from activity_controls import ActivityButton
from models import SG
from query_diagnostics import format_failure
from scanner import Scanner, ScanError, ACTIVITIES
from test_monitor import config, slot


class ActivityControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.driver = await async_playwright().start()
        self.browser = await self.driver.chromium.launch(headless=True)
        self.page = await self.browser.new_page(viewport={'width': 1280, 'height': 900})
        self.scanner = Scanner(self.directory)
        self.scanner.page = self.page
        self.destination = 'https://activesg.gov.sg/facility-bookings/activities/tennis'
        self.venue = 'https://activesg.gov.sg/facility-bookings/venues/kallang'

    async def asyncTearDown(self):
        await self.browser.close()
        await self.driver.stop()

    def options(self, exact=True, duplicate=False):
        return ('<a href="/facility-bookings/activities/mini-tennis">Mini Tennis</a>' +
                ('<a href="/facility-bookings/activities/tennis" onclick="sessionStorage.setItem(\'choice\',\'Tennis\')">Tennis</a>' if exact else '') +
                ('<a href="/facility-bookings/activities/tennis-other">Tennis</a>' if duplicate else '') +
                '<a href="/facility-bookings/activities/tennis-wall">Tennis Wall</a>')

    def group(self, options=None, opened=False, heading=None):
        options = self.options() if options is None else options
        heading = heading or ('<button id="tennis-group" aria-controls="tennis-options" aria-expanded="' + str(opened).lower() +
            '" onclick="this.setAttribute(\'aria-expanded\',\'true\');document.getElementById(\'tennis-options\').hidden=false;'
            'sessionStorage.setItem(\'expanded\',\'yes\')">Tennis</button>')
        return ('<h1>Book a facility</h1><a href="/facility-bookings/activities/table-tennis">Table Tennis</a>' +
                '<div style="height:1300px">Other activities</div><section>' + heading +
                '<div id="tennis-options"' + ('' if opened else ' hidden') + '>' + options + '</div></section>')

    async def load(self, html):
        c = config()
        label = date.fromisoformat(c.date_from).strftime('%a, %d %b')
        stamp = int(datetime.fromisoformat(c.date_from + 'T12:00').replace(tzinfo=SG).timestamp() * 1000)
        async def fixture(route):
            path = urlparse(route.request.url).path
            if path == '/facility-bookings/activities':
                body = html
            elif path == '/facility-bookings/activities/tennis':
                body = '<h1>Tennis venues</h1><a href="/facility-bookings/venues/kallang">Kallang Tennis CentreLoading...</a>'
            elif path == '/facility-bookings/venues/kallang':
                body = (f'<h1>Kallang Tennis Centre</h1><p>Each slot is 1 hour long.</p>'
                        f'<button aria-label="View timeslots for {label}" data-selected="true">date</button>'
                        f'<section><h2>Instant booking</h2><label><input type="checkbox" value="{stamp}">12:00</label></section>')
            else:
                body = '<h1>Wrong activity selected</h1>'
            await route.fulfill(content_type='text/html', body=body)
        await self.page.route('https://activesg.gov.sg/**', fixture)
        await self.page.goto(ACTIVITIES)

    async def find(self):
        self.scanner.mark_step('查找运动项目', target='Tennis')
        return await self.scanner.find_link(self.page, 'Tennis', timeout=2, poll_interval=0.02)

    async def test_collapsed_tennis_group_selects_exact_child_not_mini_or_wall(self):
        await self.load(self.group())
        button = await self.find()
        self.assertIsInstance(button, ActivityButton)
        self.assertTrue(self.scanner.progress['search_bottom_confirmed'])
        await self.scanner.click_link(self.page, button, timeout=2)
        self.assertEqual(self.page.url, self.destination)
        self.assertEqual(await self.page.evaluate("sessionStorage.getItem('choice')"), 'Tennis')
        self.assertEqual(await self.page.evaluate("sessionStorage.getItem('expanded')"), 'yes')
        self.assertEqual(self.scanner.progress['activity_options'], ['Mini Tennis', 'Tennis', 'Tennis Wall'])

    async def test_expanded_group_is_not_collapsed_again(self):
        await self.load(self.group(opened=True))
        target = await self.find()
        self.assertEqual(target, self.destination)
        await self.scanner.click_link(self.page, target, timeout=2)
        self.assertEqual(self.page.url, self.destination)
        self.assertIsNone(await self.page.evaluate("sessionStorage.getItem('expanded')"))

    async def test_aria_role_button_with_controls_is_supported(self):
        heading = '<div role="button" tabindex="0" aria-controls="tennis-options" aria-expanded="false" onclick="this.setAttribute(\'aria-expanded\',\'true\');document.getElementById(\'tennis-options\').hidden=false">Tennis</div>'
        await self.load(self.group(heading=heading))
        await self.scanner.click_link(self.page, await self.find(), timeout=2)
        self.assertEqual(self.page.url, self.destination)

    async def test_details_summary_group_is_supported(self):
        await self.load('<h1>Activities</h1><details><summary>Tennis</summary>' + self.options() + '</details>')
        await self.scanner.click_link(self.page, await self.find(), timeout=2)
        self.assertEqual(self.page.url, self.destination)

    async def test_anchor_that_expands_a_group_is_not_treated_as_navigation(self):
        heading = '<a href="#tennis-options" aria-controls="tennis-options" aria-expanded="false" onclick="event.preventDefault();this.setAttribute(\'aria-expanded\',\'true\');document.getElementById(\'tennis-options\').hidden=false">Tennis</a>'
        await self.load(self.group(heading=heading))
        button = await self.find()
        self.assertIsInstance(button, ActivityButton)
        await self.scanner.click_link(self.page, button, timeout=2)
        self.assertEqual(self.page.url, self.destination)

    async def test_direct_navigation_button_without_href_is_supported(self):
        await self.load('<h1>Activities</h1><button onclick="location.href=\'/facility-bookings/activities/tennis\'">Tennis</button>')
        await self.scanner.click_link(self.page, await self.find(), timeout=2)
        self.assertEqual(self.page.url, self.destination)

    async def test_missing_exact_child_reports_actual_options_without_selecting_other_activity(self):
        await self.load(self.group(options=self.options(exact=False)))
        button = await self.find()
        with self.assertRaisesRegex(ScanError, '名称完全一致') as caught:
            await self.scanner.click_link(self.page, button, timeout=0.7)
        await self.scanner.save_failure(caught.exception)
        self.assertEqual(self.page.url, ACTIVITIES)
        diagnostic = format_failure(self.scanner.last_failure())
        self.assertIn('分组展开：已完成', diagnostic)
        self.assertIn('Mini Tennis、Tennis Wall', diagnostic)
        self.assertNotIn('https://', diagnostic)

    async def test_ambiguous_child_destinations_are_rejected(self):
        await self.load(self.group(options=self.options(duplicate=True)))
        with self.assertRaisesRegex(ScanError, '多个同名'):
            await self.scanner.click_link(self.page, await self.find(), timeout=2)
        self.assertEqual(self.page.url, ACTIVITIES)

    async def test_group_selection_does_not_use_matching_link_outside_the_panel(self):
        heading = '<button id="tennis-group" aria-controls="tennis-options" aria-expanded="false" onclick="this.setAttribute(\'aria-expanded\',\'true\');document.getElementById(\'tennis-options\').hidden=false;document.getElementById(\'outside\').hidden=false">Tennis</button>'
        html = self.group(heading=heading) + '<a id="outside" hidden href="/facility-bookings/activities/incorrect">Tennis</a>'
        await self.load(html)
        await self.scanner.click_link(self.page, await self.find(), timeout=2)
        self.assertEqual(self.page.url, self.destination)

    async def test_delayed_suboptions_are_waited_for(self):
        heading = '<button id="tennis-group" aria-controls="tennis-options" aria-expanded="false" onclick="this.setAttribute(\'aria-expanded\',\'true\');setTimeout(() => document.getElementById(\'tennis-options\').hidden=false, 200)">Tennis</button>'
        await self.load(self.group(heading=heading))
        await self.scanner.click_link(self.page, await self.find(), timeout=2)
        self.assertEqual(self.page.url, self.destination)

    async def test_native_button_suboption_is_followed_after_expanding_group(self):
        options = self.options(exact=False) + '<button onclick="location.href=\'/facility-bookings/activities/tennis\'">Tennis</button>'
        await self.load(self.group(options=options))
        await self.scanner.click_link(self.page, await self.find(), timeout=2)
        self.assertEqual(self.page.url, self.destination)

    async def test_local_group_wrapper_without_aria_controls_is_supported(self):
        heading = '<button id="tennis-group" aria-expanded="false" onclick="this.setAttribute(\'aria-expanded\',\'true\');document.getElementById(\'tennis-options\').hidden=false">Tennis</button>'
        await self.load(self.group(heading=heading))
        await self.scanner.click_link(self.page, await self.find(), timeout=2)
        self.assertEqual(self.page.url, self.destination)

    async def test_complete_scan_follows_group_child_venue_and_reads_slots(self):
        await self.load(self.group())
        result = await self.scanner.scan(config())
        self.assertEqual(result['matches'], [slot()])
        self.assertEqual(result['results'], [{'date': config().date_from, 'status': 'checked'}])
        self.assertEqual(self.page.url, self.venue)
        self.assertEqual(await self.page.evaluate("sessionStorage.getItem('choice')"), 'Tennis')


if __name__ == '__main__':
    unittest.main()
