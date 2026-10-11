import httpx

COMMANDS = [
    ('help', '查看帮助'), ('set', '修改全部监控条件'), ('sport', '修改运动'),
    ('venue', '修改场馆'), ('dates', '修改日期'), ('time', '修改时间段'),
    ('status', '查看运行状态'), ('results', '查看完整查询结果'),
    ('diagnose', '查看失败页面证据'), ('check', '立即查询一次'), ('pause', '暂停监控'), ('resume', '恢复监控'),
]


class TelegramError(Exception):
    pass


class Telegram:
    def __init__(self, token):
        self.token = token.strip()
        self.client = httpx.AsyncClient(timeout=25)

    async def call(self, method, **payload):
        try:
            response = await self.client.post(f'https://api.telegram.org/bot{self.token}/{method}', json=payload)
            data = response.json()
        except (httpx.HTTPError, ValueError):
            # HTTP exceptions include request URLs containing the bot token.
            raise TelegramError('Telegram 网络连接失败，将自动重试。') from None
        if not data.get('ok'):
            code = data.get('error_code', response.status_code)
            descriptions = {401: 'Telegram Token 无效。', 403: '机器人无法发送消息，请在私聊中点击 Start 并取消屏蔽。',
                            409: 'Telegram 接收方式冲突，请停止同一个机器人的其他服务。', 429: 'Telegram 请求频率受限，将稍后重试。'}
            raise TelegramError(descriptions.get(code, f'Telegram 请求失败（代码 {code}）。'))
        return data['result']

    async def send(self, chat_id, text):
        return await self.call('sendMessage', chat_id=chat_id, text=text,
                               link_preview_options={'is_disabled': True})

    async def set_commands(self):
        return await self.call('setMyCommands', commands=[{'command': c, 'description': d} for c, d in COMMANDS])

    async def close(self):
        await self.client.aclose()