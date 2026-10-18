"""Regression coverage for the date strip + shared grid observed on ActiveSG."""
import asyncio
from dataclasses import replace
from datetime import date, datetime, timedelta
import os
import tempfile
import unittest
from unittest.mock import patch, AsyncMock
from urllib.parse import urlparse

from playwright.async_api import async_playwright

from engine import Engine
from models import SG, now_sg, query_summaries, slot_from_control
from scanner import ACTIVITIES, Scanner, ScanError
from storage import Store
from test_monitor import FakeScanner, FakeTelegram, config, result, slot
from test_query_feedback import message


def timestamp(day, clock):
    return int(datetime.fromisoformat(day + 'T' + clock).replace(tzinfo=SG).timestamp() * 1000)


def date_button(day, *, selected=False, disabled=False, index=0):
    # This is the real date card supplied by the user, including an EMPTY
    # data-selected attribute. Only the date and the fixture handler vary.
    label = date.fromisoformat(day).strftime('%a, %d %b').replace(', 0', ', ')
    state = (' data-selected=""' if selected else '') + (' disabled' if disabled else '')
    return f'''<button class="chakra-card css-c2kdn3" aria-label="View timeslots for {label}"{state}
      onclick="selectDate(this,{index})"><div class="chakra-card__body css-yyac81">
      <div class="chakra-stack css-t16wd"><p class="chakra-text css-10rvbm3">{label[:3]}</p>
      <p class="chakra-text css-paw7x0">{label[5:]}</p></div></div></button>'''


def time_option(day, clock, *, disabled=False, text=None):
    hour, minute = map(int, clock.split(':'))
    text = text or f'{hour % 12 or 12}:{minute:02} ' + ('pm' if hour >= 12 else 'am')
    # Real nested labels + clipped input. The label, not the 1px input, is the
    # visible option. The monitor never clicks it or attempts a reservation.
    return f'''<div class="css-6lf4k2"><label class="chakra-card css-17q8sn3" aria-hidden="false">
      <label class="chakra-card__body css-1os5zr3"><input type="checkbox" tabindex="0"
      aria-disabled="{'true' if disabled else 'false'}" value="{timestamp(day, clock)}"
      style="border:0;clip:rect(0,0,0,0);height:1px;width:1px;margin:-1px;padding:0;
      overflow:hidden;white-space:nowrap;position:absolute;">
      <p class="chakra-text css-1xypwj1">{text}</p></label></label></div>'''


class DateGridTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.driver = await async_playwright().start()
        self.browser = await self.driver.chromium.launch(headless=True)
        self.page = await self.browser.new_page()
        self.scanner = Scanner(self.directory)
        self.scanner.page = self.page
        self.day = config().date_from

    async def asyncTearDown(self):
        await self.browser.close()
        await self.driver.stop()

    async def load(self, contents):
        await self.page.route('https://activesg.gov.sg/**', lambda r: r.fulfill(
            content_type='text/html', body='<title>ActiveSG</title><h1>Select date &amp; time</h1>'
            '<p>Book instantly or ballot for slots at Kallang Tennis Centre.</p>'
            '<p>Each slot is 1 hour long. Select up to 2 slots.</p>' + contents))
        await self.page.goto(ACTIVITIES)

    async def read(self, timeout=0):
        return await self.scanner.read_slot_controls(self.page, self.day, timeout=timeout, poll_interval=0.02)

    async def test_real_empty_selection_attribute_and_common_grid_need_no_group_title(self):
        await self.load(date_button(self.day, selected=True) + time_option(self.day, '13:00'))
        controls = await self.read()
        self.assertEqual(len(controls), 1)
        self.assertEqual(controls[0]['dateConfirmed'], self.day)
        self.assertEqual(controls[0]['timeText'], '1:00 pm')
        self.assertEqual(controls[0]['bookingType'], 'displayed')
        c = replace(config(), time_from='12:00', time_to='14:00')
        self.assertEqual(slot_from_control(c, self.day, controls[0]), slot(13))
        self.assertEqual(await self.page.locator('input:checked').count(), 0)

    async def test_selected_instant_date_labels_common_grid_outside_both_date_groups(self):
        other = (date.fromisoformat(self.day) + timedelta(days=10)).isoformat()
        await self.load('<div style="display:flex"><section><div>Instant</div>'
                        + date_button(self.day, selected=True) + '</section><section><div>Ballot</div>'
                        + date_button(other) + '</section></div><div>' + time_option(self.day, '19:00') + '</div>')
        controls = await self.read()
        self.assertEqual(controls[0]['bookingType'], 'instant')

    async def test_ballot_date_is_read_and_explicitly_marked_in_report(self):
        await self.load('<section><div>Ballot</div>' + date_button(self.day, selected=True)
                        + '</section><div>' + time_option(self.day, '19:00') + '</div>')
        controls = await self.read()
        self.assertEqual(controls[0]['bookingType'], 'ballot')
        item = {**slot(19), 'booking_type': 'ballot'}
        self.assertEqual(query_summaries(result([item])), [self.day + ' 19:00（抽签）'])

    async def test_stale_grid_is_waited_for_after_date_switch(self):
        previous = (date.fromisoformat(self.day) - timedelta(days=1)).isoformat()
        await self.load(date_button(self.day, selected=True) + '<div id="grid">'
                        + time_option(previous, '19:00') + '</div>')
        await self.page.evaluate('html => setTimeout(() => grid.innerHTML=html, 150)', time_option(self.day, '20:00'))
        controls = await self.read(timeout=1)
        self.assertEqual(controls[0]['value'], str(timestamp(self.day, '20:00')))

    async def test_different_selected_date_rejects_even_a_matching_time_value(self):
        other = (date.fromisoformat(self.day) + timedelta(days=1)).isoformat()
        await self.load(date_button(other, selected=True) + time_option(self.day, '19:00'))
        with self.assertRaises(ScanError) as caught:
            await self.read()
        self.assertFalse(caught.exception.diagnostic['slots']['date_confirmed'])

    async def test_displayed_time_must_match_its_timestamp(self):
        await self.load(date_button(self.day, selected=True) + time_option(self.day, '19:00', text='8:00 pm'))
        control = (await self.read())[0]
        with self.assertRaisesRegex(ValueError, '显示时间'):
            slot_from_control(replace(config(), time_from='19:00', time_to='21:00'), self.day, control)

    async def test_explicit_empty_state_is_different_from_unloaded_grid(self):
        await self.load(date_button(self.day, selected=True) + '<div id="grid"></div>')
        with self.assertRaises(ScanError):
            await self.read()
        await self.page.locator('#grid').evaluate("n => n.textContent='No available slots for this date.'")
        self.assertEqual(await self.read(), [])

    async def test_disabled_previous_day_option_does_not_block_current_available_time(self):
        previous = (date.fromisoformat(self.day) - timedelta(days=1)).isoformat()
        await self.load(date_button(self.day, selected=True) + time_option(previous, '19:00', disabled=True)
                        + time_option(self.day, '20:00'))
        controls = await self.read()
        c = replace(config(), time_from='19:00', time_to='21:00')
        found = [slot_from_control(c, self.day, control) for control in controls]
        self.assertEqual([item['start'] for item in found if item], ['20:00'])

    async def test_date_inventory_waits_for_list_hydration(self):
        second = (date.fromisoformat(self.day) + timedelta(days=1)).isoformat()
        await self.load('<div id="dates">' + date_button(self.day, selected=True) + '</div>')
        await self.page.evaluate('html => setTimeout(() => dates.insertAdjacentHTML("beforeend",html),150)', date_button(second))
        dates = await self.scanner.date_inventory(self.page, timeout=1, poll_interval=0.02)
        self.assertEqual(len(dates), 2)

    async def test_complete_query_clicks_each_present_date_and_sends_one_summary(self):
        days = [(date.fromisoformat(self.day) + timedelta(days=index)).isoformat() for index in range(5)]
        c = replace(config(), date_to=days[-1], time_from='19:00', time_to='21:00')
        options = [time_option(days[0], clock) for clock in ('18:00', '19:00', '20:00', '21:00')]
        options.append(time_option(days[0], '19:30', disabled=True))
        # Days 3 and 4 respectively don't appear and cannot be clicked.
        dates = '<section><div>Instant</div>' + ''.join(
            date_button(days[index], selected=index == 0, disabled=index == 3, index=index)
            for index in (0, 1, 3)) + '</section><section><div>Ballot</div>' + date_button(days[4], index=4) + '</section>'
        html = '''<title>ActiveSG</title><h1>Select date &amp; time</h1>
          <p>Book instantly or ballot for slots at Kallang Tennis Centre.</p>
          <div id="dates" style="width:260px;overflow-x:auto;display:flex">''' + dates + '''</div>
          <p>Each slot is 1 hour long. Select up to 2 slots.</p><div id="grid">''' + ''.join(options) + '''</div>
          <p>Log in to book your slots.</p><button>Log in with singpass</button>
          <script>window.clickedDates=[];window.timeClicks=0;
          document.addEventListener('change',e=>{if(e.target.type==='checkbox')timeClicks++});
          function selectDate(button,index) {
            clickedDates.push(index);
            document.querySelectorAll('[data-selected]').forEach(n=>n.removeAttribute('data-selected'));
            button.setAttribute('data-selected','');
            setTimeout(()=>grid.innerHTML=window.grids[index],150);
          }</script><style>section{display:flex;flex-wrap:wrap;flex:0 0 390px}
          section:last-child{flex-basis:130px}section>div{width:100%}
          [aria-label^="View timeslots for"]{flex:0 0 130px}</style>'''
        grids = {1: time_option(days[1], '20:00'), 4: time_option(days[4], '19:00')}
        await self.page.route('https://activesg.gov.sg/**', lambda r: r.fulfill(
            content_type='text/html', body=(
                '<a href="/facility-bookings/activities/tennis/venues">Tennis</a>'
                if urlparse(r.request.url).path == urlparse(ACTIVITIES).path else
                '<a href="/facility-bookings/activities/tennis/venues/kallang/timeslots">Kallang Tennis Centre</a>'
                if urlparse(r.request.url).path.endswith('/venues') else html)))
        await self.page.add_init_script('window.grids=' + __import__('json').dumps(grids))
        self.scanner.select_day = AsyncMock(wraps=self.scanner.select_day)
        store = Store(self.directory)
        self.addCleanup(store.close)
        with patch.dict(os.environ, {'TELEGRAM_USER_ID': '', 'TELEGRAM_CHAT_ID': ''}):
            telegram = FakeTelegram()
            engine = Engine(store, self.scanner, telegram, 'activeSG_monitor_bot')
        engine.ready = True
        store.put('owner', {'user_id': '123', 'chat_id': '123'})
        with store.transaction():
            engine.change_config(c)
            engine.handle_message(message('/check'))
        await engine.run_scan()
        value = engine.runtime()['last']
        self.assertIsNotNone(value, engine.runtime()['error'])
        self.assertEqual([(r['date'], r['status']) for r in value['results']],
                         list(zip(days, ['checked', 'checked', 'not_displayed', 'disabled', 'checked'])))
        self.assertEqual([call.args[1] for call in self.scanner.select_day.await_args_list], [days[0], days[1], days[4]])
        self.assertEqual(await self.page.evaluate('clickedDates'), [1, 4])
        self.assertEqual(await self.page.evaluate('timeClicks'), 0)
        self.assertGreater(await self.page.locator('#dates').evaluate('n=>n.scrollLeft'), 0)
        self.assertEqual(len(store.db.execute('SELECT * FROM outbox').fetchall()), 1)
        delivery = asyncio.create_task(engine.notice_loop())
        try:
            for _ in range(30):
                if telegram.messages:
                    break
                await asyncio.sleep(0.02)
            self.assertEqual(len(telegram.messages), 1)
            expected = '\n'.join([
                '本次 /check 查询已完成（新加坡时间）。',
                days[0] + ' 19:00、20:00',
                days[1] + ' 20:00',
                days[2] + ' 不可查询（官网未显示该日期）',
                days[3] + ' 不可查询（官网日期按钮不可点击）',
                days[4] + ' 19:00（抽签）'])
            self.assertEqual(telegram.messages[0], ('123', expected))
            self.assertNotIn('http', expected)
        finally:
            delivery.cancel()
            await asyncio.gather(delivery, return_exceptions=True)


class DateReportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.store = Store(directory)
        self.addCleanup(self.store.close)
        self.scanner = FakeScanner()
        with patch.dict(os.environ, {'TELEGRAM_USER_ID': '', 'TELEGRAM_CHAT_ID': ''}):
            self.engine = Engine(self.store, self.scanner, FakeTelegram(), 'activeSG_monitor_bot')
        self.engine.ready = True
        self.store.put('owner', {'user_id': '123', 'chat_id': '123'})
        with self.store.transaction():
            self.engine.change_config(config())

    def queued(self):
        return [r[0] for r in self.store.db.execute('SELECT text FROM outbox ORDER BY id')]

    def clear(self):
        with self.store.transaction():
            self.store.db.execute('DELETE FROM outbox')

    async def test_missing_dates_notified_once_and_combined_with_new_times(self):
        missing = (date.fromisoformat(config().date_from) + timedelta(days=1)).isoformat()
        self.scanner.value['results'].append({'date': missing, 'status': 'not_displayed'})
        await self.engine.run_scan()
        self.assertEqual(self.queued(), [config().date_from + ' 12:00\n' + missing + ' 不可查询（官网未显示该日期）'])
        self.clear()
        await self.engine.run_scan()
        self.assertEqual(self.queued(), [])
        self.scanner.value['matches'].append(slot(13))
        await self.engine.run_scan()
        self.assertEqual(self.queued(), [config().date_from + ' 13:00'])

    async def test_unqueryable_date_does_not_erase_previous_time_baseline(self):
        await self.engine.run_scan()
        self.clear()
        self.scanner.value = result([])
        self.scanner.value['results'][0]['status'] = 'not_displayed'
        await self.engine.run_scan()
        self.assertEqual(self.engine.runtime()['seen'], [slot()['key']])
        self.clear()
        self.scanner.value = result([slot()])
        await self.engine.run_scan()
        self.assertEqual(self.queued(), [])

    async def test_manual_query_returns_all_times_once_even_when_already_reported(self):
        self.scanner.value = result([slot(), slot(13)])
        await self.engine.run_scan()
        self.clear()
        with self.store.transaction():
            self.engine.handle_message(message('/check'))
        await self.engine.run_scan()
        self.assertEqual(self.queued(), ['本次 /check 查询已完成（新加坡时间）。\n' + config().date_from + ' 12:00、13:00'])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM outbox WHERE kind='slots'").fetchone()[0], 0)

    async def test_manual_empty_results_explain_missing_dates(self):
        self.scanner.value = result([])
        self.scanner.value['results'][0]['status'] = 'not_displayed'
        with self.store.transaction():
            self.engine.handle_message(message('/check'))
        await self.engine.run_scan()
        self.assertIn('不可查询（官网未显示该日期）', self.queued()[0])
        self.assertNotIn('无符合条件', self.queued()[0])

    async def test_results_lists_checked_empty_and_old_unavailable_status(self):
        next_day = (date.fromisoformat(config().date_from) + timedelta(days=1)).isoformat()
        self.scanner.value = result([])
        self.scanner.value['results'].append({'date': next_day, 'status': 'not_selectable'})
        await self.engine.run_scan()
        text = self.engine.full_results()[0]
        self.assertIn(config().date_from + ' 无符合条件的时间点', text)
        self.assertIn(next_day + ' 不可查询（官网暂不可选）', text)

    async def test_failed_round_never_delivers_partial_times_or_clears_missing_date_state(self):
        await self.engine.run_scan()
        old = self.engine.runtime()['last']
        self.clear()
        self.scanner.error = ScanError('第二个日期仍在加载。')
        await self.engine.run_scan()
        self.assertEqual(self.engine.runtime()['last'], old)
        self.assertTrue(all('12:00' not in item for item in self.queued()))


if __name__ == '__main__':
    unittest.main()
