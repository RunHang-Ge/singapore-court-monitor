import asyncio
from dataclasses import replace
import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from engine import Engine
from storage import Store
from test_monitor import FakeScanner, FakeTelegram, config
from test_query_feedback import message


ENV = {'TELEGRAM_USER_ID': '', 'TELEGRAM_CHAT_ID': '', 'BROWSERLESS_TOKEN': 'test-browser-token',
       'KALLANG_LOGIN_USERNAME': 'private-account@example.invalid', 'KALLANG_LOGIN_PASSWORD': 'private-site-password'}


class FakePortal:
    busy = False
    live = False
    def __init__(self):
        self.started = asyncio.Event()
        self.release = None
        self.value = {'status': 'ready', 'message': '已读取场地和日期。', 'headings': ['Facility booking'],
                      'venues': ['Tennis Court 1'], 'dates': ['2026-10-21'], 'filters': ['Date']}
    def valid_handoff(self, identifier):
        return self.live and identifier == 'valid'
    async def inspect(self, on_human=None):
        self.busy = True
        try:
            self.started.set()
            if self.release:
                await self.release.wait()
            return dict(self.value)
        finally:
            self.busy = False
    async def close(self):
        pass


class KallangEngineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, ENV))
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.store = Store(directory)
        self.addCleanup(self.store.close)
        self.scanner, self.telegram = FakeScanner(), FakeTelegram()
        self.scanner.lock = asyncio.Lock()
        self.scanner.release_browser = AsyncMock()
        self.engine = Engine(self.store, self.scanner, self.telegram, 'activeSG_monitor_bot')
        self.engine.kallang = FakePortal()
        self.engine.ready = True
        self.store.put('owner', {'user_id': '123', 'chat_id': '123'})
        with self.store.transaction():
            self.engine.change_config(config())

    def request(self):
        with self.store.transaction():
            return self.engine.handle_message(message('/kallang'))[1][0]

    async def test_command_is_private_and_leaves_activesg_conditions_unchanged(self):
        before_config, before_runtime = self.engine.config(), self.engine.runtime()
        wrong = message('/kallang')
        wrong['message']['from']['id'] = wrong['message']['chat']['id'] = 999
        self.assertEqual(self.engine.handle_message(wrong), (None, None))
        self.assertIn('一次性登录检查', self.request())
        self.assertIn('已安排或正在进行', self.request())
        self.assertTrue(self.engine.kallang_trigger.is_set())
        self.assertEqual(self.engine.config(), before_config)
        self.assertEqual(self.engine.runtime(), before_runtime)

    async def test_missing_credentials_never_start_a_browser(self):
        with patch.dict(os.environ, {'KALLANG_LOGIN_PASSWORD': ''}):
            reply = self.request()
        self.assertIn('KALLANG_LOGIN_PASSWORD', reply)
        self.assertFalse(self.engine.kallang_trigger.is_set())
        self.assertFalse(self.engine.kallang.started.is_set())
        self.assertNotIn('private-account', reply)

    async def test_inspection_queues_one_report_and_does_not_start_a_second_monitor(self):
        self.request()
        before = self.engine.runtime()
        await self.engine.run_kallang()
        self.scanner.release_browser.assert_awaited_once()
        self.assertEqual(self.engine.runtime(), before)
        rows = self.store.db.execute('SELECT text,kind FROM outbox').fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['kind'], 'kallang:result')
        self.assertIn('Tennis Court 1', rows[0]['text'])
        self.assertIn('2026-10-21', rows[0]['text'])
        self.assertNotIn('private-account', json.dumps(dict(rows[0])))
        self.assertEqual(self.store.get('kallang_result')['status'], 'ready')

    async def test_existing_monitor_waits_for_new_site_and_then_continues(self):
        portal = self.engine.kallang
        portal.release = asyncio.Event()
        inspection = asyncio.create_task(self.engine.run_kallang())
        await portal.started.wait()
        scan = asyncio.create_task(self.engine.run_scan())
        try:
            await asyncio.sleep(0.02)
            self.assertFalse(scan.done())
            self.assertFalse(self.engine.busy)
            self.assertIsNone(self.engine.runtime()['error'])
            portal.release.set()
            await asyncio.gather(inspection, scan)
            self.assertIsNotNone(self.engine.runtime()['last'])
            self.assertIsNone(self.engine.runtime()['error'])
        finally:
            for task in (inspection, scan):
                task.cancel()
            await asyncio.gather(inspection, scan, return_exceptions=True)

    async def test_new_site_results_survive_activesg_condition_changes(self):
        old_revision = self.engine.runtime()['revision']
        with self.store.transaction():
            self.store.queue(['old ActiveSG result'], old_revision)
            self.store.queue(['new site observation'], old_revision, 'kallang:result')
            self.engine.change_config(replace(config(), time_from='19:00', time_to='21:00'))
        notice = self.store.next_notice(self.engine.runtime()['revision'])
        self.assertEqual(notice['text'], 'new site observation')
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], 1)

    async def test_restart_discards_live_entry_but_keeps_completed_report(self):
        revision = self.engine.runtime()['revision']
        with self.store.transaction():
            self.store.queue(['old live entry'], revision, 'kallang:human:valid')
            self.store.queue(['completed observation'], revision, 'kallang:result')
        Engine(self.store, self.scanner, self.telegram, 'activeSG_monitor_bot')
        notice = self.store.next_notice(revision)
        self.assertEqual(notice['text'], 'completed observation')

    async def test_expired_live_entry_is_not_delivered(self):
        revision = self.engine.runtime()['revision']
        with self.store.transaction():
            self.store.queue(['expired temporary URL'], revision, 'kallang:human:expired')
            self.store.queue(['finished'], revision, 'kallang:result')
        task = asyncio.create_task(self.engine.notice_loop())
        try:
            for _ in range(25):
                if self.telegram.messages:
                    break
                await asyncio.sleep(0.02)
            self.assertEqual(self.telegram.messages, [('123', 'finished')])
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


class KallangWebTests(unittest.TestCase):
    def test_admin_and_csrf_protect_login_request_without_returning_secrets(self):
        from fastapi.testclient import TestClient
        from main import app
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            **ENV, 'TELEGRAM_USER_ID': '123', 'TELEGRAM_CHAT_ID': '123',
            'DATA_DIR': directory, 'ADMIN_PASSWORD': 'long-test-admin-password',
            'TELEGRAM_BOT_TOKEN': '',
        }), patch.object(Engine, 'start'):
            with TestClient(app) as client:
                engine = app.state.engine
                engine.ready = True
                self.assertEqual(client.post('/kallang/inspect').status_code, 401)
                auth = ('admin', 'long-test-admin-password')
                self.assertEqual(client.post('/kallang/inspect', auth=auth).status_code, 403)
                response = client.post('/kallang/inspect', auth=auth, headers={'x-monitor-csrf': app.state.csrf})
                self.assertEqual(response.status_code, 200)
                self.assertTrue(engine.kallang_pending)
                status = client.get('/status', auth=auth)
                self.assertTrue(status.json()['kallang_busy'])
                self.assertNotIn('private-site-password', status.text + response.text)
                self.assertNotIn('private-account', status.text + response.text)


if __name__ == '__main__':
    unittest.main()
