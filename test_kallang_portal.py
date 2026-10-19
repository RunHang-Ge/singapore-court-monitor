import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from playwright.async_api import async_playwright

from kallang_portal import HOST, KallangPortal


LOGIN_FIXTURE = '''<!doctype html><html><body>
<h1>The Kallang</h1>
<form onsubmit="return false">
  <input name="Login" placeholder="Email">
  <input name="Password" type="password" placeholder="Password">
  <label><input type="checkbox" id="remember">Remember me</label>
  <div id="submit">Login</div>
</form>
<script>
document.getElementById('submit').onclick = () => {
  if(document.querySelector('[name=Login]').value !== 'fixture-account@example.invalid' ||
     document.querySelector('[name=Password]').value !== 'private-fixture-password' ||
     document.getElementById('remember').checked) {
    document.body.innerHTML = '<div role="alert">Invalid credentials</div>';
    return;
  }
  history.replaceState({}, '', '#/Dashboard');
  document.body.innerHTML = '<button>Logout</button><button id="facility">Book a facility</button>';
  document.getElementById('facility').onclick = () => {
    history.replaceState({}, '', '#/Bookings');
    document.body.innerHTML = '<button>Logout</button><h1>Facility booking</h1>' +
      '<h2>The Kallang Tennis Courts</h2><label>Court</label><button>2026-10-21</button>' +
      '<button>2026-10-22</button><h3>Welcome fixture-account</h3>' +
      '<input value="private-fixture-password"><button onclick="window.accidentalBooking=true">Confirm booking</button>';
  };
};
</script></body></html>'''


class FixturePortal(KallangPortal):
    async def _start(self):
        self.deadline = time.monotonic() + 30
        self.driver = await async_playwright().start()
        self.browser = await self.driver.chromium.launch(headless=True, timeout=10000)
        self.context = await self.browser.new_context()
        self.page = await self.context.new_page()
        await self.page.route('https://' + HOST + '/**',
                              lambda route: route.fulfill(body=LOGIN_FIXTURE, content_type='text/html'))
        await self.page.goto('https://' + HOST + '/clientportal2/#/Login')


class KallangPortalTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_off_login_reads_real_booking_screen_without_reserving(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            'KALLANG_LOGIN_USERNAME': 'fixture-account@example.invalid',
            'KALLANG_LOGIN_PASSWORD': 'private-fixture-password',
        }):
            portal = FixturePortal(directory)
            observed = {}
            cleanup = portal._cleanup

            async def cleanup_with_assertion():
                if portal.page is not None:
                    observed['booked'] = await portal.page.evaluate('!!window.accidentalBooking')
                await cleanup()

            portal._cleanup = cleanup_with_assertion
            result = await portal.inspect()
            self.assertEqual(result['status'], 'ready')
            self.assertIn('The Kallang Tennis Courts', result['venues'])
            self.assertEqual(result['dates'], ['2026-10-21', '2026-10-22'])
            self.assertFalse(observed['booked'])
            self.assertFalse(portal.busy)
            self.assertIsNone(portal.driver)
            self.assertIsNone(portal.page)
            self.assertIsNone(portal.handoff)
            self.assertEqual(portal.profile.path.parent, Path(directory) / 'kallang')
            serialized = json.dumps(result)
            self.assertNotIn('fixture-account', serialized)
            self.assertNotIn('private-fixture-password', serialized)

    async def test_url_change_or_exception_cannot_claim_success_or_leak_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            portal = KallangPortal(directory)
            self.assertFalse(portal._ready({'login_form': False, 'signed_in': True,
                                           'booking_area': True, 'headings': ['Facility booking']}))
            self.assertFalse(portal._ready({'login_form': True, 'booking_area': True,
                                           'venues': ['Tennis Courts'], 'dates': ['2026-10-21']}))

            async def failure():
                raise RuntimeError('wss://provider.invalid?token=private-token private-password')

            portal._start = failure
            with self.assertNoLogs():
                result = await portal.inspect()
            self.assertEqual(result['status'], 'error')
            self.assertNotIn('private-token', json.dumps(result))
            self.assertNotIn('private-password', json.dumps(result))
            self.assertIsNone(portal.driver)
            self.assertFalse(portal.busy)

    async def test_sanitized_provider_error_is_distinct_from_login_failure(self):
        from remote_browser import RemoteBrowserError
        with tempfile.TemporaryDirectory() as directory:
            portal = KallangPortal(directory)
            async def failure():
                raise RemoteBrowserError('Browserless 会话创建失败：额度不足或已有会话占用；请查看 Browserless 账号。')
            portal._start = failure
            result = await portal.inspect()
            self.assertEqual(result['status'], 'error')
            self.assertIn('额度不足或已有会话占用', result['message'])
            self.assertNotEqual(result['status'], 'login_failed')

    def test_public_booking_preview_is_not_authenticated_success(self):
        with tempfile.TemporaryDirectory() as directory:
            portal = KallangPortal(directory)
            preview = {'login_form': False, 'signed_in': False, 'booking_area': True,
                       'venues': ['The Kallang Tennis Courts'], 'dates': ['2026-10-21']}
            self.assertFalse(portal._ready(preview))
            self.assertEqual(portal._result('ready', 'unverified success', preview)['status'], 'needs_human')

    def test_authenticated_booking_data_can_be_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            portal = KallangPortal(directory)
            observed = {'login_form': False, 'signed_in': True, 'booking_area': True,
                        'venues': ['The Kallang Tennis Courts'], 'dates': ['2026-10-21']}
            self.assertTrue(portal._ready(observed))
            self.assertEqual(portal._result('ready', 'confirmed', observed)['status'], 'ready')
            observed['signed_in'] = False
            portal._submitted = True
            self.assertTrue(portal._ready(observed))
            observed['venues'] = []
            self.assertFalse(portal._ready(observed))

    async def test_reused_account_redaction_cannot_leave_empty_ready_result(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            'KALLANG_LOGIN_USERNAME': 'fixture-account@example.invalid',
        }):
            portal = KallangPortal(directory)
            captured = {}

            async def start_without_login():
                captured['username_before_form'] = portal._username
                raise RuntimeError('stop before contacting any service')

            portal._start = start_without_login
            await portal.inspect()
            self.assertEqual(captured['username_before_form'], 'fixture-account@example.invalid')
            portal._username = captured['username_before_form']
            observed = {'login_form': False, 'signed_in': True, 'booking_area': True,
                        'venues': ['fixture-account Tennis Courts'], 'dates': ['2026-10-21']}
            result = portal._result('ready', 'must not stay ready after redaction', observed)
            self.assertEqual(result['venues'], [])
            self.assertEqual(result['status'], 'signed_in')
            self.assertNotIn('fixture-account', json.dumps(result))


if __name__ == '__main__':
    unittest.main()
