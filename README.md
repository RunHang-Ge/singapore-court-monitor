# Singapore Court Monitor

Python 3 / FastAPI 的 ActiveSG 空场监控，连接现有 Telegram 机器人 `@activeSG_monitor_bot`。
目标 Render 服务：https://singapore-court-monitor.onrender.com

## 已实现的行为

- 按运动、场馆、日期范围、每天时间段查询 ActiveSG；每 10 分钟开始一轮，上一轮尚未结束时不重叠。
- 点击官网实际提供的日期，读取下方公共区域可选的一小时开始时间；例如 19:00–21:00 匹配 7:00 pm 和 8:00 pm。识别到抽签日期时会注明“抽签”。不执行预约、登录或付款。
- 完成整轮后汇总通知：每天一行，例如 `2026-10-19 19:00、20:00`，采用新加坡时间 24 小时制，不附链接。
- 自动通知只包含相比上次成功查询新增或重新出现的时段。相同和仅减少的时段保持静默。
- 请求范围内官网未显示的日期、不可点击的日期和已过日期分别注明不可查询；不可查询不等同于没有空场。变化的不可查询日期也会通知一次，不每轮重复。
- `/check` 完成后直接发送本轮完整列表；`/results` 返回最近一轮完整结果，两者不受自动时段去重影响。
- SQLite 保存条件、去重基线、Webhook 更新和待发送通知。发送失败重试，重启后继续。
- 查询失败保留上次成功结果，尝试通知一次故障；恢复时通知。Telegram 本身不可连接时不能发送故障通知。
- 修改条件后丢弃尚未完成的旧查询；暂停后仍可接收指令。
- 日期到期后停止查询并通知。
- 私聊账号绑定，其他账号、群聊、转发和编辑消息不能修改条件。

## The Kallang：先验证登录与预约页面

此阶段只接入一次性登录检查，尚未为 PerfectGym 新站启用日期规则或定时查询。
Render 环境变量新增 `KALLANG_LOGIN_USERNAME`（邮箱账号）、`KALLANG_LOGIN_PASSWORD`；账号密码不发送到聊天、GitHub 或日志。继续使用已有的 `BROWSERLESS_TOKEN`。

推送新版本并等待 Render Live 后，在已绑定的 Telegram 私聊发送 `/kallang`，或在受管理员密码保护的首页点击“登录并查看场地日期”。程序会等当前浏览器操作完成，使用新站独立档案登录，只点击实际可见的场地预约导航，不提交预约或付款。若有验证或页面仍需操作，会发送短期远程浏览器入口；在同一页面完成登录、打开场地与日期，点击 Done 后仍需以实际界面确认，不能仅凭 Done 宣称成功。

检查结果只报告有限的预约标题、场地名称、可见日期和筛选项。`/kallang_status` 或管理首页可查看本次结果。必须实际读到具体场地和日期才能标记已确认；仅完成登录时会说明未读取完整。收到实际页面信息后，再确认监控日期规则。

新站档案位于 `/var/data/kallang`，与 ActiveSG 分开；两站浏览器操作排队以避免远程并发和内存增长。登录页不截图、不保存完整 HTML、账号密码或输入值。免费 Browserless 单次连接时限仍适用；入口到期后需重新发送 `/kallang`。部署只新增检查能力，不会自动登录或开启第二项监控。ActiveSG 的条件和十分钟监控保持原有行为。

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
/browser
/check
/pause
/resume
/help
```

也可直接发送 `Tennis / Kallang Tennis Centre / 日期范围 / 时间范围`。
最多 31 天、同一天内至少一小时；所有时间按新加坡解释。日期列表加载后一次读取所有已渲染按钮（包含横向画面外的按钮），对照完整请求日期范围；缺失日期注明“官网未显示该日期”，禁用按钮注明“官网日期按钮不可点击”，过去日期注明“日期已过期”。其余日期逐个原生点击，并核对选中日期及时间选项内的 Unix 时间戳；日期不一致时等待新数据，不沿用上一天仍在显示的时间。
`/check` 与 `/browser` 的每次手动请求会回复本轮成功或失败；成功后直接发送整轮完整结果，包含全部匹配的时间点、已查询但没有匹配时间的日期，以及不可查询日期，每天一行。同一轮不再另外发送一份新增时间通知。`/results` 返回同样的缓存结果。定时扫描只发送新增或重新出现的时间点及变化的不可查询日期；没有变化则保持静默。不可查询日期保留之前的时间比较基线，避免日期重新可查时错误重复播报。重复故障只提示一次。
如果汇总超出 Telegram 单条消息长度限制，会按整天分成少量连续消息，不截断或丢弃时段。

## 人工处理验证：Telegram 远程操作入口

此路线保持 Render 的 Python 3 Web Service，Chrome 在 Browserless 中运行。设置好变量后构建会跳过 Render 本机 Chromium 的安装和启动；Render 无需启动桌面窗口。主服务仍需保持单实例、一个 worker。

在 Render → Environment 设置：

| 名称 | 值 |
|---|---|
| BROWSERLESS_TOKEN | 在 Browserless 账号中取得，只填 Render，不发送到聊天或 GitHub |
| BROWSER_PROVIDER | browserless（缺少 Token 时直接报告配置问题，避免意外启动本机 Chrome） |
| BROWSERLESS_REGION | sfo（可省略；也可选 lon、ams，切换区域会创建新档案） |
| BROWSERLESS_TIMEOUT_SECONDS | 120（免费账号保持默认；增大需要对应账号支持） |

`BROWSER_PROVIDER` 未设置时默认 auto：有 BROWSERLESS_TOKEN 自动使用 Browserless，否则使用原有本机模式。已设置的 BROWSER_CDP_URL 在 Browserless 模式下不使用。不要将 BROWSER_HEADLESS 改成 false，远程模式自身使用有界面的 Chrome。

部署流程：GitHub Desktop → Commit（若代码已提交则跳过）→ Push origin；Render 自动部署或 Manual Deploy → Deploy latest commit，等待 Live。

在 @activeSG_monitor_bot 私聊中：

1. 如果已暂停，发送 `/resume`；发送 `/browser` 立即安排查询。
2. 网站检查等待约五秒仍未完成时，机器人发送临时远程操作入口和准确到期时间。打开入口即可操作同一个云端浏览器；iPhone 可通过 Safari 打开。入口通常不足 90 秒，收到后立即打开。
3. 在远程画面中手动完成检查。程序会检查页面实际状态，只有恢复官网场地页面后才继续提取数据；点击 Done 不代表验证已通过。不自动登录、预约、付款或调用验证码自动代答。
4. 处理期间不要 `/pause`；暂停或修改条件会取消旧入口。处理失败/入口过期后发送 `/browser` 重试；还在有效期内时 `/browser` 会重新发送同一入口，不延长时间。
5. 发送 `/status` 看查询是否成功；`/results` 查看整轮结果。出现验证入口不表示已取得场地数据。如果未生成入口或后续读取失败，会回复本轮原因。

入口发给已绑定的私聊账号，不含服务 API Token，但持有入口的人可操作该页面，不要转发。程序不记录直播或额外保存登录画面；生成失败、过期或重启后的入口不会从通知队列重新发出。浏览器会话断开后远程 Cookie/缓存尽量保留在同一档案，下次查询复用；网站也可能因验证到期或连接条件变化再次要求检查。

Browserless 免费方案的单次浏览器连接最长两分钟；入口不能延长该连接。代码为人工处理和后续查询预留时间，但日期较多或响应缓慢时仍可能超时，整轮未完成不会通知部分场地结果。免费方案每月 1000 units，每次新连接至少消耗 1 unit；每 10 分钟持续运行 30 天有 4320 轮，免费额度无法覆盖全天整月监控。此版本先用免费额度验证人工处理是否有效，不自动购买套餐或付费代理。

远程档案最长保留一天，并在 /var/data/browserless-profile.json 保存可复用连接（含凭据，文件权限 0600，Git 已忽略）。该文件不通过管理接口公开；部署重启保留档案，过期/Token 变化后创建新档案。不要分享该文件。Browserless 清理远程档案不等于 ActiveSG 验证有效期。

**待实际验证的限制**：ActiveSG 仍可能拒绝 Browserless 的云端来源，Cloudflare 检查也不一定提供可以手动点击的选项。只有从真实官网成功完成整轮查询、且下轮能复用状态后，才能确认路线有效；单纯健康检查和本地测试通过不能确认。

原有本机截图页面保留为调试备用：先暂停并等待查询结束，再刷新截图操作。它与 Telegram 的临时直播入口不是同一人工流程。若没有 Browserless，本机模式仍使用原有 45 秒自动等待和每轮关闭浏览器的策略，复杂页面加载峰值可能超过 512 MB。

活动与场馆列表会先持续向下滚动，即使已在页面中找到目标链接，也会继续滚到底部。到达底部后，会等待列表高度稳定并重复确认无法继续向下滚动；若加载出新内容，则继续滚动。页面状态检查将标题、正文和安全标记合并为一次远程读取；本地列表查找默认最多 12 秒，远程模式默认最多 30 秒，并根据会话剩余时间缩短、至少预留 15 秒给后续操作。仍保留本轮查询与远程会话的超时限制，超时未到底时明确报告“尚未确认列表到底”，不采用部分结果。

运动项目支持直接链接和需要先展开的分组按钮，并识别实际官网中的 Chakra 卡片展开行（div.chakra-stack + p + svg）。同一张卡片的 chakra-collapse 区域用于查找子选项，忽略折叠区域内尚未显示的链接。Tennis 分组展开后，会在该分组内精确选择同名 Tennis，不选择 Mini Tennis 或 Tennis Wall；若同名子选项缺失或不唯一，报告实际选项并停止本轮查询。

## 确认安全检查的来源

发送 /diagnose 获取最近一次失败时保存的证据。活动或场馆匹配失败时，还会显示查找阶段、目标名称与页面读取到的候选名称，以及目标文字实际所在的有限元素层级；操作失败时会显示实际阶段、等待步骤、耗时、滚动查找次数、目标按钮是否进入画面和点击状态；日期与时间读取失败时，也会显示选中日期是否一致、分组文字、页面标题文字和控件数量；仅记录官网 facility-bookings 页面中的这些有限信息，不保存完整正文、登录字段或带参数的地址。服务首页「上次查询失败时的证据」可显示页面标题、来源网址（已去除查询参数）、主页面 HTTP 状态、Cloudflare 标记和失败截图；读取证据不会启动浏览器。查询失败后才会保存，因此上线这个版本后需要再查询一次。

- cf-mitigated: challenge 明确证明收到 Cloudflare 整页检查；检查页中的 _cf_chl_opt 也是其明确标记。
- CF-Ray 或 Server: cloudflare 单独出现只能说明经过 Cloudflare，不能证明拦截。Cloudflare 脚本或品牌文字与检查文字同时出现时仍标为「疑似」。
- 网页内嵌 Cloudflare 验证框与整页检查分别标示；实际跳转到登录 URL 时单独标示登录。
- 只有一般检查文字时保留「来源尚未确认」；空白页不会当成安全检查。
- 「刷新画面」是当前浏览器访问，可能与失败时不同；应优先看保存的证据。

证据保存在持久磁盘的 last-query-failure.json / .jpg（各保留最新一份）。不保存完整 HTML、Cookie、表单内容或所有响应头。登录页不截图；图片只通过管理员认证可读取。程序仍不会自动完成验证码，增加诊断不表示查询已恢复。

## 512 MB 实例的内存排查

查询无论成功或失败，完成后关闭本地 Chromium 和 Playwright 驱动。浏览器 Cookie 保留在持久磁盘，下次查询重新打开。每轮仍只使用一个浏览器、一个页面、一个查询任务和一个 Uvicorn worker。
手动浏览器操作闲置两分钟后关闭，也可点击「关闭浏览器释放内存」。查看运行状态不会启动浏览器。远程 CDP 浏览器仅断开连接，不关闭它原有的页面。

管理首页显示容器当前内存和浏览器状态；Render 日志每 30 秒以及浏览器启动/释放时写入 `memory phase=...`。数值为 MiB：`container_mib` 是容器总占用，`python_mib` 仅为网页服务的 Python 进程，不能拿后者判断是否达到容器限额；`peak_mib` 是容器存续期间历史峰值，释放内存后也不会下降。平台未提供的数据为 null。

部署后先 `/pause`，关闭管理页的浏览器，记录待机内存；再 `/resume` 或 `/check`，观察一次查询及结束后的内存。Render → 服务 → Metrics → Application Metrics 可查看内存曲线。提供曲线上的待机值、查询峰值、重启时间，以及附近的 `memory` 日志即可，不需要密码或 Token。

释放待机浏览器不能保证启动或复杂网页加载峰值低于 512 MB。若一次查询仍超过限额，需要更大运行内存或将浏览器移到其他服务。Build Pipeline 的内存属于构建资源，不会增加 Web Service 的运行内存。

## 本地开发与验证

本地开发不需要常驻旧版 Node 服务。

```powershell
cd F:\Github\singapore-court-monitor
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe -m unittest -v test_monitor test_remote_browser test_query_feedback test_slot_reader test_navigation test_scrolling test_activity_controls test_chakra_activities test_remote_latency test_date_time_flow test_kallang_portal test_kallang_engine
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
- `scanner.py`：官网活动与场馆可见按钮点击、页面切换核验与分步骤诊断、日期按钮切换、活动与场馆卡片滚动查找与加载等待、场馆名称与预览状态分离、名称唯一核验、日期核验、时段分组加载等待、时段读取、人工接管和浏览器操作。
- `remote_browser.py`：Browserless 档案创建与复用、临时入口校验和关闭。
- `activity_controls.py`：活动链接与展开按钮识别、可见名称精确匹配、按钮定位。
- `page_snapshot.py`：一次读取页面标题、正文和安全标记，减少远程往返并保持证据来自同一文档。
- `page_scrolling.py`：活动与场馆列表的分页滚动查找、内部滚动容器处理、目标按钮位置确认。
- `date_controls.py`：日期按钮、选中/禁用状态和加载状态的一次读取，支持官网空值 `data-selected` 属性。
- `slot_reader.py`：同一次浏览器读取中获取选中日期、公共区域时间选项及页面状态。使用真实嵌套 label 和时间戳，不要求时间选项必须位于 Instant/Ballot 标题内；识别到抽签分组时保留标记。
- `test_date_time_flow.py`：用户提供的真实日期和时间控件 HTML、横向日期栏、缺失/禁用日期、日期切换残留时间、整轮 Telegram 发送和去重。
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
- Browserless 临时入口：https://docs.browserless.io/api-reference/cdp-extensions
- Browserless 档案保留：https://docs.browserless.io/baas/session-management/persisting-state
- Browserless 免费额度和连接时限：https://www.browserless.io/pricing
