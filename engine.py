import asyncio
from dataclasses import replace
from datetime import timedelta
import hmac
import json
import os
import re
import secrets

from remote_browser import RemoteBrowserError
from query_diagnostics import format_failure
from models import Config, now_sg, parse_dates, parse_set, parse_window, summaries, query_summaries, unavailable_dates
from telegram_api import TelegramError
from kallang_portal import KallangPortal

HELP = '''可用指令：
/set Tennis / Kallang Tennis Centre / 2026-10-12~2026-10-18 / 19:00-22:00
/sport Tennis
/venue Kallang Tennis Centre
/dates 2026-10-12~2026-10-18
/time 19:00-22:00（也接受 7pm-10pm）
/status 查看条件与最近查询
/results 返回最近一轮完整结果
/diagnose 查看失败时保存的页面证据
/browser 获取人工验证入口（通过 Telegram 发送）
/check 立即查询一次
/kallang 登录 The Kallang 并查看场地日期（一次性）
/kallang_status 查看新站登录检查结果
/pause 暂停；/resume 恢复

时间点为一小时场次的开始时间，例如 19:00-21:00 匹配 19:00、20:00。每轮完成后每天一行，不附链接；页面未提供的日期会注明不可查询。/check 返回本轮完整结果；自动提醒只包含新增或重新出现的时间点，及变化的不可查询日期。'''


class Engine:
    def __init__(self, store, scanner, telegram, expected_username, public_url='', mode='webhook', binding_code=''):
        self.store, self.scanner, self.telegram = store, scanner, telegram
        self.expected_username = expected_username.lstrip('@')
        self.public_url, self.mode = public_url.rstrip('/'), mode
        self.binding_code = binding_code or store.get('binding_code') or secrets.token_urlsafe(24)
        if not store.get('binding_code'):
            store.put('binding_code', self.binding_code)
        self.ready, self.connection_error, self.delivery_error = False, None, None
        self.trigger = asyncio.Event()
        self.browser_gate = asyncio.Lock()
        self.kallang = KallangPortal(store.directory)
        self.kallang_trigger = asyncio.Event()
        self.kallang_pending = False
        self.tasks = []
        self.busy, self.next_check = False, None
        self.scan_revision = None
        self.stopping = False
        # A process restart cannot restore the live page or its temporary access.
        with store.transaction():
            store.db.execute("DELETE FROM outbox WHERE kind LIKE 'human:%' OR kind LIKE 'kallang:human:%'")
        owner = os.getenv('TELEGRAM_USER_ID', '').strip()
        chat = os.getenv('TELEGRAM_CHAT_ID', '').strip() or owner
        if owner:
            if not owner.isdecimal() or not chat.isdecimal() or owner != chat:
                raise ValueError('本工具仅绑定个人私聊：TELEGRAM_USER_ID 和 TELEGRAM_CHAT_ID 应为同一个正整数。')
            existing = store.get('owner')
            if existing and existing['user_id'] != owner:
                raise ValueError('环境变量中的 Telegram 用户与已绑定用户不一致。')
            store.put('owner', {'user_id': owner, 'chat_id': chat})
        if binding_code and len(binding_code) < 24:
            raise ValueError('BINDING_CODE 至少需要 24 个字符。')

    def runtime(self):
        return self.store.get('runtime')

    def config(self):
        data = self.store.get('config')
        return Config(**data).validate() if data else None

    def start(self):
        self.tasks = [asyncio.create_task(self.connect_loop()), asyncio.create_task(self.update_loop()),
                      asyncio.create_task(self.notice_loop()), asyncio.create_task(self.monitor_loop()),
                      asyncio.create_task(self.kallang_loop())]

    async def close(self):
        self.stopping = True
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.scanner.close()
        await self.kallang.close()
        await self.telegram.close()
        self.store.close()

    async def connect_loop(self):
        if not self.telegram.token:
            self.connection_error = '请在 Render 设置 TELEGRAM_BOT_TOKEN。'
            return
        while not self.stopping:
            try:
                bot = await self.telegram.call('getMe')
                if bot.get('username', '').casefold() != self.expected_username.casefold():
                    self.connection_error = 'Token 对应的机器人不是 @' + self.expected_username + '，未连接。'
                    return
                if self.mode == 'webhook':
                    if not self.public_url.startswith('https://'):
                        self.connection_error = '请设置 PUBLIC_URL，或在 Render 中等待 RENDER_EXTERNAL_URL 可用。'
                        return
                    await self.telegram.call('setWebhook', url=self.public_url + '/telegram/webhook',
                                             secret_token=self.store.get('webhook_secret'),
                                             allowed_updates=['message'], drop_pending_updates=False,
                                             max_connections=1)
                elif self.mode == 'polling':
                    hook = await self.telegram.call('getWebhookInfo')
                    if hook.get('url'):
                        self.connection_error = '机器人已有 webhook；本地模式不会接管云端机器人。'
                        return
                else:
                    self.connection_error = 'TELEGRAM_MODE 应为 webhook 或 polling。'
                    return
                await self.telegram.set_commands()
                self.ready, self.connection_error = True, None
                self.trigger.set()
                if self.mode == 'polling':
                    await self.poll_loop()
                return
            except TelegramError as error:
                self.connection_error = str(error)
                await asyncio.sleep(15)
            except Exception:
                self.connection_error = 'Telegram 初始化失败，请检查配置。'
                await asyncio.sleep(15)

    async def poll_loop(self):
        while not self.stopping:
            try:
                updates = await self.telegram.call('getUpdates', offset=self.store.get('poll_offset', 0),
                                                   timeout=10, allowed_updates=['message'])
                for update in updates:
                    self.store.accept_update(update)
                    self.store.put('poll_offset', update['update_id'] + 1)
                self.connection_error = None
            except TelegramError as error:
                self.connection_error = str(error)
                await asyncio.sleep(5)

    def status(self):
        config, rt = self.config(), self.runtime()
        owner = self.store.get('owner')
        state = '尚未设置条件' if config is None else '已到期' if rt['completed'] else '正在查询' if self.busy else '监控运行中' if rt['enabled'] else '已暂停'
        handoff = getattr(self.scanner, 'handoff', None)
        if handoff and self.scanner.valid_handoff(handoff['id']):
            state = '等待人工处理网站验证（入口已安排发送到 Telegram）'
        elif rt['enabled'] and rt['error'] and not self.busy:
            state = '查询受阻，等待重试'
        lines = [config.text() if config else '尚未设置监控条件，请发送 /set。', '状态：' + state]
        if handoff and self.scanner.valid_handoff(handoff['id']):
            lines.append('人工入口到期：' + handoff['expires_at'])
        if not owner:
            lines.append('尚未绑定接收账号。')
        if self.next_check and rt['enabled']:
            lines.append('下次查询：' + self.next_check.strftime('%Y-%m-%d %H:%M:%S'))
        if rt['last']:
            lines.append('上次成功查询：' + rt['last']['checked_at'][:19].replace('T', ' ') + f"，符合条件的时段 {len(rt['last']['matches'])} 个")
        if rt['error']:
            lines.append('查询问题：' + rt['error'])
        if self.delivery_error or self.connection_error:
            lines.append('消息通道：' + (self.delivery_error or self.connection_error))
        pending = self.store.db.execute('SELECT COUNT(*) FROM outbox WHERE sent=0 AND revision=?', (rt['revision'],)).fetchone()[0]
        if pending:
            lines.append(f'等待发送的通知：{pending} 条')
        return '\n'.join(lines)

    def full_results(self):
        rt = self.runtime()
        if not rt['last']:
            if rt['error']:
                return ['当前条件尚无成功查询结果。\n最近一次失败：' + rt['error'] +
                        '\n发送 /diagnose 查看本轮失败证据，/browser 或 /check 重试。']
            return ['当前条件尚未完成查询。' + ('正在查询，请等待本轮完成。' if self.busy else '发送 /check 开始查询。')]
        last = rt['last']
        messages = query_summaries(last, '查询时间：' + last['checked_at'][:19].replace('T', ' ') + '（新加坡时间）')
        suffix = ''
        if rt['error']:
            suffix += '\n最近一次查询失败，以上为上次成功结果。'
        if suffix:
            if len(messages[-1] + suffix) < 4096:
                messages[-1] += suffix
            else:
                messages.append(suffix.strip())
        return messages

    def change_config(self, candidate):
        candidate.validate(future=True)
        previous = self.config()
        if previous == candidate:
            return '条件未变。\n' + candidate.text()
        rt = self.runtime()
        rt.update(revision=rt['revision'] + 1, seen=[], last=None, error=None,
                  error_notified=False, completed=False, last_notification=None)
        if previous is None:
            rt['enabled'] = True
        self.store.set('manual_query', None)
        self.store.set('config', candidate.dict())
        self.store.set('runtime', rt)
        self.trigger.set()
        return '监控条件已保存。\n' + candidate.text() + ('\n已开始监控。' if rt['enabled'] else '\n目前已暂停，发送 /resume 开始。')

    def handle_message(self, update):
        message = update.get('message')
        if not isinstance(message, dict) or message.get('chat', {}).get('type') != 'private':
            return None, None
        sender = message.get('from', {})
        uid, chat = str(sender.get('id', '')), str(message['chat'].get('id', ''))
        if sender.get('is_bot') or not uid.isdecimal() or uid != chat or message.get('forward_origin'):
            return None, None
        text = str(message.get('text', '')).strip()
        if not text or len(text) > 2000:
            return None, None
        first, _, argument = text.partition(' ')
        command, _, target = first.partition('@')
        if target and target.casefold() != self.expected_username.casefold():
            return None, None
        command = command.lower()
        owner = self.store.get('owner')
        if not owner:
            if command != '/start' or not self.binding_code or not hmac.compare_digest(argument.strip(), self.binding_code):
                return None, None
            self.store.set('owner', {'user_id': uid, 'chat_id': chat})
            self.trigger.set()
            return chat, ['已绑定 @' + self.expected_username + '。\n发送 /set 设置条件，/help 查看帮助。']
        if uid != owner['user_id'] or chat != owner['chat_id']:
            return None, None
        try:
            if command in ('/start', '/help') or command == '/set' and not argument:
                reply = HELP
            elif command == '/kallang':
                reply = self.request_kallang()
            elif command == '/kallang_status':
                reply = self.kallang_status()
            elif command == '/status':
                reply = self.status()
            elif command == '/diagnose':
                reply = format_failure(self.scanner.last_failure())
            elif command == '/results':
                return chat, self.full_results()
            elif command == '/set':
                reply = self.change_config(parse_set(argument))
            elif command in ('/sport', '/venue', '/dates', '/time'):
                config = self.config()
                if not config:
                    raise ValueError('请先用 /set 设置完整条件。')
                if not argument.strip():
                    raise ValueError('请在指令后填写参数。\n' + HELP)
                if command == '/sport':
                    candidate = replace(config, activity=argument.strip())
                elif command == '/venue':
                    candidate = replace(config, venue=argument.strip())
                elif command == '/dates':
                    first_date, last_date = parse_dates(argument)
                    candidate = replace(config, date_from=first_date, date_to=last_date)
                else:
                    start, end = parse_window(argument)
                    candidate = replace(config, time_from=start, time_to=end)
                reply = self.change_config(candidate)
            elif command in ('/pause', '/resume'):
                config, rt = self.config(), self.runtime()
                enabled = command == '/resume'
                if enabled and (not config or config.expired()):
                    raise ValueError('请先用 /set 或 /dates 设置未来的条件。')
                if enabled != rt['enabled']:
                    rt.update(enabled=enabled, revision=rt['revision'] + 1, completed=False)
                    self.store.set('manual_query', None)
                    self.store.set('runtime', rt)
                self.trigger.set()
                reply = '监控已恢复。' if enabled else '监控已暂停，仍可修改条件。'
            elif command in ('/check', '/browser'):
                config, rt = self.config(), self.runtime()
                if not config or config.expired():
                    raise ValueError('请先设置未来的监控条件。')
                if not rt['enabled']:
                    raise ValueError('监控已暂停，请先发送 /resume。')
                if command == '/browser':
                    from remote_browser import provider
                    if provider() != 'browserless':
                        raise ValueError('人工入口需要 Render 中的 BROWSERLESS_TOKEN，并使用 BROWSER_PROVIDER=browserless。')
                handoff = getattr(self.scanner, 'handoff', None)
                if command == '/browser' and handoff and self.scanner.valid_handoff(handoff['id']):
                    self.store.set('manual_query', {'revision': rt['revision'], 'command': command})
                    self.store.queue([self.scanner.human_message()], rt['revision'], 'human:' + handoff['id'])
                    reply = '已安排重新发送当前人工操作入口；请尽快打开，入口不会延长。'
                else:
                    # Manual requests get their own outcome. Automatic fault
                    # suppression must not make an explicit retry look ignored.
                    self.store.set('manual_query', {'revision': rt['revision'], 'command': command})
                    if self.busy and self.scan_revision == rt['revision']:
                        reply = '正在查询；已记录你的手动请求，本轮会回复成功或失败。需要人工处理时会另发操作入口。'
                    else:
                        self.trigger.set()
                        reply = ('已安排人工入口查询；需要验证时会发送入口，未生成入口也会回复本轮原因。'
                                 if command == '/browser' else '已安排一次查询；完成后按日期汇总时间点，并列出不可查询的日期。')
            elif '/' in text and not text.startswith('/'):
                reply = self.change_config(parse_set(text))
            else:
                reply = '无法识别这条指令。\n' + HELP
            return chat, [reply]
        except (TypeError, ValueError, RemoteBrowserError) as error:
            return chat, [str(error)]

    async def update_loop(self):
        while not self.stopping:
            row = self.store.next_update() if self.ready else None
            if row is None:
                await asyncio.sleep(0.3)
                continue
            try:
                if not row['handled']:
                    with self.store.transaction():
                        chat, reply = self.handle_message(json.loads(row['payload']))
                        self.store.handled(row['id'], chat, json.dumps({'messages': reply or [], 'index': 0}, ensure_ascii=False) if reply else None)
                        # Binding codes need not remain in the stored incoming payload.
                        self.store.db.execute('UPDATE updates SET payload=? WHERE id=?', ('{}', row['id']))
                    row = self.store.db.execute('SELECT * FROM updates WHERE id=?', (row['id'],)).fetchone()
                    if row['done']:
                        continue
                progress = json.loads(row['reply'])
                while progress['index'] < len(progress['messages']):
                    await self.telegram.send(row['chat_id'], progress['messages'][progress['index']])
                    progress['index'] += 1
                    with self.store.transaction():
                        self.store.db.execute('UPDATE updates SET reply=? WHERE id=?', (json.dumps(progress, ensure_ascii=False), row['id']))
                self.store.update_done(row['id'])
                self.delivery_error = None
            except TelegramError as error:
                self.delivery_error = str(error)
                await asyncio.sleep(5)
            except Exception:
                self.delivery_error = '指令处理失败，正在重试。'
                await asyncio.sleep(5)

    async def notice_loop(self):
        while not self.stopping:
            owner, rt = self.store.get('owner'), self.runtime()
            notice = self.store.next_notice(rt['revision']) if owner and self.ready else None
            if notice is None:
                await asyncio.sleep(0.5)
                continue
            try:
                if notice['kind'].startswith('human:') and not self.scanner.valid_handoff(notice['kind'][6:]):
                    with self.store.transaction():
                        self.store.sent_notice(notice['id'])
                    continue
                if notice['kind'].startswith('kallang:human:') and not self.kallang.valid_handoff(notice['kind'][14:]):
                    with self.store.transaction():
                        self.store.sent_notice(notice['id'])
                    continue
                receipt = await self.telegram.send(owner['chat_id'], notice['text'])
                with self.store.transaction():
                    self.store.sent_notice(notice['id'])
                    latest = self.runtime()
                    if latest['revision'] == notice['revision'] and notice['kind'] == 'slots':
                        latest['last_notification'] = {'sent_at': now_sg().isoformat(), 'message_id': receipt['message_id']}
                        self.store.set('runtime', latest)
                self.delivery_error = None
            except TelegramError as error:
                self.delivery_error = str(error)
                await asyncio.sleep(5)
            except Exception:
                self.delivery_error = '通知保存失败，正在重试。'
                await asyncio.sleep(5)

    def current(self, revision):
        rt = self.runtime()
        return rt['enabled'] and rt['revision'] == revision

    def manual_request(self, revision):
        request = self.store.get('manual_query')
        return request if request and request.get('revision') == revision else None

    async def run_scan(self):
        # A one-off Kallang inspection and the existing monitor share one
        # browser slot. Waiting here avoids a false ActiveSG failure/extra Chrome.
        async with self.browser_gate:
            return await self._run_scan()

    async def _run_scan(self):
        config, rt = self.config(), self.runtime()
        if not config or not rt['enabled'] or not self.store.get('owner') or not self.ready:
            return
        revision = rt['revision']
        self.busy = True
        self.scan_revision = revision
        self.next_check = None
        try:
            if config.expired():
                with self.store.transaction():
                    rt.update(enabled=False, completed=True)
                    self.store.set('runtime', rt)
                    self.store.queue(['监控已结束：指定日期和时间已过期。'], revision, 'status')
                    if self.manual_request(revision):
                        self.store.set('manual_query', None)
                return
            async def notify_human(handoff):
                if self.current(revision) and self.scanner.valid_handoff(handoff['id']):
                    with self.store.transaction():
                        self.store.queue([self.scanner.human_message()], revision, 'human:' + handoff['id'])
            self.scanner.on_human = notify_human
            self.scanner.is_current = lambda: self.current(revision) and not self.stopping
            result = await self.scanner.scan(config)
            if not self.current(revision):
                return
            latest = self.runtime()
            new = [s for s in result['matches'] if s['key'] not in latest['seen']]
            manual = self.manual_request(revision)
            unavailable = unavailable_dates(result)
            previous_unavailable = unavailable_dates(latest['last'] or {})
            changed_dates = {day: reason for day, reason in unavailable.items()
                             if previous_unavailable.get(day) != reason}
            # An unqueryable date is unknown, rather than proof that a previously
            # reported time disappeared. Keep that date's comparison baseline.
            seen = sorted({s['key'] for s in result['matches']} |
                          {key for key in latest['seen'] if key.split('|')[0] in unavailable})
            with self.store.transaction():
                if manual:
                    self.store.queue(query_summaries(result, f"本次 {manual['command']} 查询已完成（新加坡时间）。"),
                                     revision, 'manual')
                    self.store.set('manual_query', None)
                else:
                    messages = summaries(new, changed_dates)
                    if latest['error_notified']:
                        if messages:
                            messages[0] = 'ActiveSG 查询已恢复。\n' + messages[0]
                        else:
                            messages = ['ActiveSG 查询已恢复。']
                    if messages:
                        self.store.queue(messages, revision, 'slots' if new else 'status')
                latest.update(last=result, seen=seen, error=None, error_notified=False)
                self.store.set('runtime', latest)
        except Exception as error:
            if self.current(revision):
                from scanner import ScanError
                text = str(error) if isinstance(error, ScanError) else '查询发生错误，未采用本轮结果。'
                latest = self.runtime()
                with self.store.transaction():
                    latest['error'] = text
                    manual = self.manual_request(revision)
                    if manual:
                        self.store.queue([f"本次 {manual['command']} 未完成查询：" + text +
                                          '\n发送 /diagnose 查看本轮失败证据。'], revision, 'manual')
                        self.store.set('manual_query', None)
                        latest['error_notified'] = True
                    elif not latest['error_notified']:
                        self.store.queue(['ActiveSG 监控需要处理：' + text + '\n修复后将自动重试。'], revision, 'status')
                        latest['error_notified'] = True
                    self.store.set('runtime', latest)
        finally:
            self.scanner.on_human = None
            self.scanner.is_current = lambda: True
            self.busy = False
            self.scan_revision = None

    def request_kallang(self):
        if not self.store.get('owner'):
            raise ValueError('请先绑定 Telegram 私聊，再开始 The Kallang 登录检查。')
        if not self.ready:
            raise ValueError('请等待 Telegram 连接成功后再开始新站登录检查，以便接收需要人工操作时的入口。')
        if self.kallang_pending or self.kallang.busy:
            return 'The Kallang 登录检查已安排或正在进行，请等待本轮结果；/kallang_status 查看状态。'
        if not os.getenv('KALLANG_LOGIN_USERNAME', '').strip() or not os.getenv('KALLANG_LOGIN_PASSWORD', ''):
            raise ValueError('请在 Render 设置 KALLANG_LOGIN_USERNAME 和 KALLANG_LOGIN_PASSWORD；账号密码不要发送到聊天。')
        if not os.getenv('BROWSERLESS_TOKEN', '').strip():
            raise ValueError('请先在 Render 设置 BROWSERLESS_TOKEN，再开始新站登录检查。')
        self.kallang_pending = True
        self.kallang_trigger.set()
        return '已安排 The Kallang 一次性登录检查；当前浏览器操作结束后开始，场地和日期结果会统一发给你。需要人工操作时会发送临时入口。'

    @staticmethod
    def format_kallang(result):
        lines = ['The Kallang 登录检查', result['message']]
        for key, label in (('headings', '预约页面'), ('venues', '页面场地'), ('dates', '可见日期'), ('filters', '可见筛选项')):
            if result.get(key):
                lines.append(label + '：' + '、'.join(result[key][:20]))
        lines.append('目前仅检查登录和页面，尚未启用新站监控。')
        # Each field is bounded by the portal reader; keep the whole Telegram
        # report in one message even if the site exposes many public labels.
        return '\n'.join(lines)[:3800]

    def kallang_status(self):
        if self.kallang_pending:
            return 'The Kallang 登录检查已排队，等待当前浏览器操作完成。'
        if self.kallang.busy:
            return 'The Kallang 登录检查正在进行；需要人工操作时会通过 Telegram 发送入口。'
        result = self.store.get('kallang_result')
        return self.format_kallang(result) if result else '尚未进行 The Kallang 登录检查；发送 /kallang 开始。'

    async def run_kallang(self):
        async def notify_human(handoff):
            if self.kallang.valid_handoff(handoff['id']) and not self.stopping:
                with self.store.transaction():
                    self.store.queue(['The Kallang 临时浏览器入口（新加坡时间 ' + handoff['expires_at'] + ' 到期）：\n' +
                                      handoff['url'] + '\n请在同一页面完成登录并进入场地日期页面，完成后点 Done。'],
                                     self.runtime()['revision'], 'kallang:human:' + handoff['id'])
        async with self.browser_gate:
            async with self.scanner.lock:
                # Release the prior site's idle remote connection before using
                # another profile; the ActiveSG cookie/profile stays intact.
                await self.scanner.release_browser()
                self.kallang_pending = False
                result = await self.kallang.inspect(on_human=notify_human)
        result['checked_at'] = now_sg().isoformat()
        with self.store.transaction():
            self.store.set('kallang_result', result)
            self.store.queue([self.format_kallang(result)], self.runtime()['revision'], 'kallang:result')

    async def kallang_loop(self):
        while not self.stopping:
            await self.kallang_trigger.wait()
            self.kallang_trigger.clear()
            try:
                await self.run_kallang()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Never forward browser exceptions or environment values.
                result = {'status': 'error', 'message': 'The Kallang 登录检查未完成，请稍后发送 /kallang 重试。'}
                with self.store.transaction():
                    self.store.set('kallang_result', result)
                    self.store.queue([self.format_kallang(result)], self.runtime()['revision'], 'kallang:result')
            finally:
                self.kallang_pending = False

    async def monitor_loop(self):
        self.trigger.set()
        while not self.stopping:
            self.trigger.clear()
            rt = self.runtime()
            if rt['enabled'] and self.ready and self.store.get('owner') and self.config():
                revision = rt['revision']
                started = asyncio.get_running_loop().time()
                await self.run_scan()
                if self.trigger.is_set() or self.runtime()['revision'] != revision:
                    continue
                remaining = max(1, 600 - (asyncio.get_running_loop().time() - started))
                self.next_check = now_sg() + timedelta(seconds=remaining) if self.runtime()['enabled'] else None
            else:
                self.next_check = None
                remaining = 600
            try:
                await asyncio.wait_for(self.trigger.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                pass