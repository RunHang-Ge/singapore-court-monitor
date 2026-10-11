# Singapore Court Monitor

Python 3 / FastAPI 的 ActiveSG 空场监控，连接现有 Telegram 机器人 `@activeSG_monitor_bot`。
目标 Render 服务：https://singapore-court-monitor.onrender.com

## 已实现的行为

- 按运动、场馆、日期范围、每天时间段查询 ActiveSG；每 10 分钟开始一轮，上一轮尚未结束时不重叠。
- 任意完整一小时在即时预约分组可选即可，不执行预约、登录或付款。
- 完成整轮后汇总通知：每天一行，例如 `2026-10-12 12:00–13:00、13:00–14:00；`，不附链接。
- 自动通知只包含相比上次成功查询新增或重新出现的时段。相同和仅减少的时段保持静默。
- `/results` 返回最近一轮完整结果，不受自动去重影响。
- SQLite 保存条件、去重基线、Webhook 更新和待发送通知。发送失败重试，重启后继续。
- 查询失败保留上次成功结果，尝试通知一次故障；恢复时通知。Telegram 本身不可连接时不能发送故障通知。
- 修改条件后丢弃尚未完成的旧查询；暂停后仍可接收指令。
- 日期到期后停止查询并通知。
- 私聊账号绑定，其他账号、群聊、转发和编辑消息不能修改条件。

## 现有 Render Web Service 配置

本项目使用 Render 的原生 **Python 3** 运行环境，不需要将语言改为 Docker。
在现有 Web Service → Settings 中设置：

| 设置 | 值 |
|---|---|
| Repository | `RunHang-Ge/singapore-court-monitor` |
| Branch | `main`（以实际仓库分支为准） |
| Root Directory | 留空 |
| Language | Python 3 |
| Build Command | `bash build.sh` |
| Start Command | `bash start.sh` |
| Health Check Path | `/healthz` |
| 实例数量 | 1 |

`.python-version` 使用 Python 3.13 的最新补丁版本。如果已有 PYTHON_VERSION 环境变量，它的优先级更高，需确认仍为受支持的 Python 3 版本。
不要使用多个 uvicorn workers，也不要在第二台机器同时运行同一个机器人的轮询。

### 必需环境变量

| 名称 | 填写方式 |
|---|---|
| `TELEGRAM_BOT_TOKEN` | 你已设置的 @activeSG_monitor_bot Token；只填在 Render，不提交到 GitHub |
| `ADMIN_PASSWORD` | 自己设置至少 16 字符的管理密码，管理页面用户名固定为 `admin` |
| `DATA_DIR` | `/var/data`，同时在 Render Disk 中挂载持久磁盘到该路径 |

以下是可选变量：

- `TELEGRAM_BOT_USERNAME=activeSG_monitor_bot`：默认已设置，用于核对 Token 对应的机器人。
- `TELEGRAM_MODE=webhook`：默认 webhook。
- `PUBLIC_URL=https://singapore-court-monitor.onrender.com`：可省略，默认读取 Render 自动提供的 `RENDER_EXTERNAL_URL`。
- `TELEGRAM_USER_ID`：如知道自己的 Telegram 数字用户 ID，可以填入并直接绑定个人私聊；`TELEGRAM_CHAT_ID` 可省略，默认与用户 ID 相同。
- `BINDING_CODE`：如希望自己设定首次绑定码，填写至少 24 字符。否则自动生成并显示在受保护的管理页面。
- `BROWSER_HEADLESS=true`：默认值。原生服务不能直接启动桌面窗口，不要随意改成 false。
- `BROWSER_CDP_URL`：可选远程浏览器连接地址；仅在部署环境无法运行本地 Chromium 时使用。不提交带凭据的地址。

Hobby 是工作区套餐，Build Pipeline Starter 是构建配置。运行实例另行设置。Free Web Service 会在缺乏入站请求时休眠，后台访问 ActiveSG 不会可靠地保持其运行，不能满足持续十分钟监控；需要使用常驻付费实例。持久磁盘也需付费实例。不要把截图中的构建 CPU/内存当作运行资源。

### 首次上线和绑定

1. 在 Render 完成上述设置和持久磁盘配置。
2. 将代码推送 GitHub；自动部署未开启时选择 Manual Deploy → Deploy latest commit。
3. 等待部署成功。访问 `/healthz`：`telegram_connected: true` 表示已核实 Token 对应用户名并成功注册 webhook。
4. 打开服务首页，用用户名 `admin` 和 ADMIN_PASSWORD 登录。
5. 如果未设置 TELEGRAM_USER_ID，页面会显示 `/start 随机绑定码`。复制整条指令，在 **@activeSG_monitor_bot 的个人私聊**发送一次。
6. 收到绑定成功回复后，发送 `/help`，然后发送实际未来条件，例如：

   ```text
   /set Tennis / Kallang Tennis Centre / 2026-10-12~2026-10-18 / 12:00-14:00
   ```

7. 第一次设置完整条件会开始监控。已有条件暂停或到期后修改不会自动恢复，需 `/resume`。
8. `/status` 查看本轮状态；`/results` 获取完整上轮结果。确认官网实际查询成功后再视为监控可用。

本地实现完成不等于 Render 已上线。只有 Render 成功构建、服务启动、Webhook 注册和私聊绑定成功后，机器人才能工作。

## Telegram 指令

```text
/set Tennis / Kallang Tennis Centre / 2026-10-12~2026-10-18 / 19:00-22:00
/sport Tennis
/venue Kallang Tennis Centre
/dates 2026-10-12~2026-10-18
/time 19:00-22:00
/time 7pm-10pm
/status
/results
/check
/pause
/resume
/help
```

也可直接发送 `Tennis / Kallang Tennis Centre / 日期范围 / 时间范围`。
最多 31 天、同一天内至少一小时；所有时间按新加坡解释。不可选日期会标记为未确认，不会使用官网跳转到其他日期的数据。
`/check` 立即安排查询，但遵循去重规则；需要完整结果时使用 `/results`。
如果汇总超出 Telegram 单条消息长度限制，会按整天分成少量连续消息，不截断或丢弃时段。

## 浏览器验证和原生环境限制

`build.sh` 安装 Playwright 的 Chromium 并运行启动诊断。原生 Python 环境是否具备 Chromium 需要的系统库，必须在实际 Render 构建中验证。仅安装 Python 包不能保证浏览器可运行。

- 构建显示浏览器启动失败：将日志中缺少的系统库名称提供给维护者；如果原生环境无法提供这些库，需要配置远程浏览器，或者重新讨论部署方式。不要反复增加 pip 包来代替系统库。
- 程序会先等待网站自动完成安全检查，最多 45 秒；不会因短暂显示检查页而立即报告失败。仍未完成时才提示查询受阻。
- ActiveSG 显示 Cloudflare 验证：在机器人发 `/pause`，等正在进行的查询结束。登录服务首页，刷新浏览器画面，手动点击页面按钮，必要时向下滚动。处理后发 `/resume`。
- 管理画面使用云端 Chromium 的截图和输入操作，不会绕过网站验证。部分验证在无头浏览器或云端 IP 上始终无法通过，这种情况下查询仍然不可用。
- 官网要求 Singpass 或跳到外部页面时不会自动登录。服务也不保存 Singpass 密码。
- 不能仅凭“部署成功”或健康检查成功认定读取到了预约数据。

## 本地开发与验证

本地开发不需要常驻旧版 Node 服务。

```powershell
cd F:\Github\singapore-court-monitor
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe -m unittest -v test_monitor
.\.venv\Scripts\python.exe browser_diagnostic.py --check
```

复制 `.env.example` 为 `.env` 后可配置本地开发。**不要用已经注册云端 webhook 的机器人启动本地服务**，以免接管云端 webhook。
本地仅测试时不填 Token，使用 `DATA_DIR=./data`。

```powershell
.\.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000
```

真实网页诊断（不发送 Telegram、不预约）：

```powershell
.\.venv\Scripts\python.exe browser_diagnostic.py --visit
```

## 代码文件

- `main.py`：网页服务、管理页权限、Telegram webhook。
- `engine.py`：指令、定时查询、结果比较和通知队列。
- `scanner.py`：官网导航、日期核验、时段读取及浏览器操作。
- `models.py`：参数解析、新加坡时间、一小时筛选、通知格式。
- `storage.py`：SQLite 持久化。
- `telegram_api.py`：Telegram API，错误中不泄露 Token。
- `dashboard.html`：受密码保护的状态和浏览器页面。
- `test_monitor.py`：去重、重启重试、权限、旧查询丢弃与浏览器 DOM 测试。

## 官方参考

- Render Python/FastAPI：https://render.com/docs/deploy-fastapi
- Render 持久磁盘：https://render.com/docs/disks
- Render 免费实例限制：https://render.com/docs/free
- Playwright 浏览器依赖：https://playwright.dev/python/docs/browsers
- Telegram webhook：https://core.telegram.org/bots/api#setwebhook