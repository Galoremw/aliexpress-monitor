# AliExpress 竞品监控 MVP

用于选品研究的 AliExpress 公开数据监控系统。商品日销量只采用商品页 IXSPY 图表中用户可见的日增量；不采集累计售出数，也不通过累计值差分估算。店铺汇总只覆盖已添加并保持活跃的监控商品，不代表店铺真实后台订单量或全店销量。

## MVP 能力

- 手动添加同行店铺和商品 URL/ID
- 输入店铺链接后，从公开店铺页发现商品并自动建立监控列表
- HTTP 仅采集公开商品页可用的非销量字段；销量由已登录 Chrome 读取 IXSPY 可见日增量
- 解析失败照常保存 Snapshot，不覆盖历史记录
- 按 `Asia/Shanghai` 自然日保存 IXSPY 直接日增量并汇总店铺监控范围
- APScheduler 每日采集，以及单商品/全量手动采集
- FastAPI JSON API、OpenAPI 文档和轻量中文 Dashboard
- 监控台内置「产品定价表」：普货 WPS 核价、PLA/PETG 3D 打印核价和独立纸箱包装预测
- AUTO FIRST + BROWSER FALLBACK：HTTP 失败后由专用 Chrome 顺序采集，验证页只允许人工正常通过
- PostgreSQL + Alembic + Docker Compose 部署

系统不实现登录、验证码绕过、浏览器指纹规避、代理轮换或其他平台安全机制规避。若公开页面拒绝访问或结构无法识别，会保存失败快照供排查。

## Windows 本地部署

日常使用不依赖 Codex Preview。Windows 安装并启动 Docker Desktop 后，双击项目目录中的 `start.bat`，脚本会启动 PostgreSQL、Backend、Frontend 和 Scheduler，等待健康检查通过后，在默认浏览器打开普通监控台：

<http://127.0.0.1:3000>

此地址不启用浏览器采集 worker。每日采集使用单独的 Chrome 配置，避免触碰日常 Chrome 的窗口和标签页。

停止使用时双击 `stop.bat`；需要重新构建或重启时双击 `restart.bat`。PostgreSQL 数据保存在 Docker volume `postgres_data`，执行 `docker compose down` 不会删除该 volume。

项目目录中还提供 `AliExpress Monitor.url`，可以右键发送到桌面作为网页快捷方式。网页快捷方式要求服务已经运行；希望双击时自动启动 Docker 的场景，请为 `open-monitor.bat` 创建桌面快捷方式。

店小秘任务本身不再额外启动 Chrome 配置或窗口；它复用当前选中的采集电脑上已打开的监控台 worker 窗口，并在同一窗口打开商品标签。`aliexpress-monitor-collector://start` 是 Galore 日常浏览器采集的独立启动入口，不由店小秘任务调用，也不能改变后端选中的采集电脑。首次运行 `start.bat` 会为当前 Windows 用户注册本机 `aliexpress-monitor://` 启动协议；浏览器首次询问是否打开外部应用时选择允许。

## Hosted Frontend and Backend

GitHub Pages 部署的是 `frontend/` 下的静态前端，生产构建通过 GitHub Actions 变量 `BACKEND_URL` 连接托管 FastAPI。前端使用 Hash 路由，打开 Pages 不需要当前电脑运行 Docker、FastAPI 或 PostgreSQL。数据库、Scheduler、Collector 和 Chrome Extension Backend 都运行在托管 Backend 所在的服务器。

GitHub Actions 文件为 `.github/workflows/deploy-pages.yml`，在 `main` 分支 push 或手动触发时执行 `npm ci`、`npm run build`、`npm test`，然后使用 GitHub Pages 官方 Actions 发布 `frontend/dist`。

### Environment Variables

`VITE_API_BASE_URL` 只能填写 Backend origin，例如 `https://monitor-api.example.com`，不能放 API key、数据库密码、Cookie、Token 或其他秘密。到 GitHub 仓库 Settings -> Secrets and variables -> Actions -> Variables 中创建：

```text
BACKEND_URL=https://你的后端域名
```

工作流会拒绝空地址、本机地址和 `127.0.0.1` 地址，避免发布一个无法连接的 Pages。Backend 必须提供 `GET /health`，并通过 HTTPS 对外服务。

### Backend Deployment

GitHub Pages 只托管静态 Frontend；FastAPI、PostgreSQL、APScheduler 和 AliExpress Collector 应运行在云服务器或受控 Backend 环境中。服务器部署时设置 `BACKEND_BIND_HOST=0.0.0.0`，并在防火墙或反向代理层限制访问。PostgreSQL 不在 Compose 中对公网发布，建议 Backend 使用托管 PostgreSQL 或服务器内网数据库。

将 Backend 的 `FRONTEND_ALLOWED_ORIGINS` 设置为 Pages 的 origin，例如：

```text
FRONTEND_ALLOWED_ORIGINS=https://galoremw.github.io,http://127.0.0.1:3000
```

如果使用自定义前端域名，把它加入同一逗号分隔列表。部署后先访问 `https://你的后端域名/health`，再打开 Pages。

### Architecture

```text
GitHub Pages -> hosted FastAPI + PostgreSQL + scheduler
Cloud Docker -> FastAPI + PostgreSQL + scheduler + collectors
Chrome Extension -> hosted FastAPI browser-extension endpoint
```

本地 Compose 固定地址：

- Frontend: <http://127.0.0.1:3000>
- Backend: <http://127.0.0.1:8000>
- API 文档: <http://127.0.0.1:8000/docs>

## Render 云端部署

根目录的 `render.yaml` 提供 Render Blueprint：FastAPI Web Service、独立 Scheduler Worker、静态 Frontend 和 Render PostgreSQL。创建 Blueprint 时填写：

- `FRONTEND_ALLOWED_ORIGINS`: GitHub Pages 或 Render Frontend 的 HTTPS 地址
- `VITE_API_BASE_URL`: Backend 的 HTTPS 地址，例如 `https://aliexpress-monitor-api.onrender.com`
- `FIRECRAWL_API_KEY`: 只有启用 Firecrawl fallback 时才填写

Backend 使用 `/health` 做健康检查，数据库迁移由部署前命令执行，不会清空已有数据。Render PostgreSQL 的标准 `postgresql://` 连接串会在应用内转换为 `postgresql+psycopg://`，本地 Docker Compose 不受影响。

## Docker Compose 启动

```bash
docker compose up -d --build
```

本地启动后访问：

- Dashboard: <http://localhost:8000/>
- 产品定价表: <http://localhost:8000/pricing>（监控台登录后使用，不需要在页面输入 API Token）
- API 文档: <http://localhost:8000/docs>
- 健康检查: <http://localhost:8000/health>

产品定价表使用监控台内的固定规则和 WPS 纸箱目录。纸箱预测只根据产品重量、尺寸匹配包装并显示预计总重量，不会改变普货或 3D 打印的定价及运费计算。跨容器调用使用 `PRICING_SERVICE_TOKEN`，仅配置在服务端环境变量中；浏览器页面使用监控台登录会话。

## Chrome Extension 手动补采

扩展目录为 `extension/`。在 Chrome 打开 `chrome://extensions`，开启“开发者模式”，选择“加载已解压的扩展程序”并选中该目录。每日 worker profile 由计划任务启动器加载扩展；手动补采仍可在你选择的 Chrome 配置中加载扩展，然后从监控台商品详情页点击“在 Chrome 打开”，在商品页点击扩展图标中的“采集当前商品”。

扩展只读取当前商品页的公开可见信息，并通过 `POST /api/collection/browser-extension` 写入新的 `ProductSnapshot(source=CHROME_EXTENSION)`；它不会读取 Cookie、绕过验证码或后台自动监控页面。自动采集失败的商品可在 `/collection/pending` 查看并补采。

## Hosted Chrome Extension

扩展的 API 地址在 [extension/config.js](extension/config.js) 中配置。将其中的 `http://127.0.0.1:8000` 改成同一个托管 Backend origin，并在 `extension/manifest.json` 的 `host_permissions` 中加入该 Backend 的精确 origin，例如：

```json
"https://monitor-api.example.com/*"
```

不要使用 `https://*/*` 放宽权限。修改后在 `chrome://extensions` 重新加载扩展。扩展仍然需要在用户自己的 Chrome 配置中正常登录 AliExpress；它不会把 Cookie 或登录凭据上传到 Backend。

## 每日浏览器自动采集

双击 `install-browser-collector.bat` 会创建 Windows 计划任务 `AliExpress Monitor Daily Collection`，每天北京时间 `15:15` 调用 `start-browser-collector.bat`。如果计划时间电脑未开机，Windows 会在当前用户下次登录后尽快补跑。计划任务确保本地服务运行，并启动独立 Chrome 用户配置 `%LOCALAPPDATA%\AliExpressMonitor\BrowserCollectorProfile`；它不会关闭、复用或改动日常 Chrome 配置。

首次启用时，运行一次 `start-browser-collector.bat`，从任务栏打开新出现的独立 Chrome 窗口，在该配置中登录监控台、AliExpress 和 IXSPY。启动器通过 Chrome 的 `--load-extension` 加载项目 `extension/`；无需把 worker 参数加到日常监控台。独立配置完成登录后可保持运行，计划任务会复用它；如果尚未运行，计划任务会单独启动它。采集窗口保持可见但不抢键盘焦点；若遇到验证，才会将验证页置前。普通 Chrome 可以照常使用。

在 Galore 主机本机通过 `http://127.0.0.1:3000` 打开监控台，可点击“Galore主机采集”启动或唤起该独立 Chrome；按钮只负责启动浏览器，不创建或启动采集队列。`start.bat` 会注册按钮所需的本机启动协议。

每日 `15:15` 调度准备待启动的店铺页队列，并以 IXSPY 已截止的最新数据日作为窗口末日；采集不会自动打开店铺或商品页面。上海时间 15:00 前，数据窗口截止到前两天；15:00 起，截止到前一天。要运行队列，在监控台“每日浏览器采集”中选择一台在线采集电脑，再单独点击“开始采集”；只有所选电脑可以领取任务，没有自动切换或并行采集。切换电脑时需等旧任务结束，再重新选择并点击开始。`立即采集全部` 与“重新检查今日任务”只准备或刷新队列，不会启动采集。

开始后，扩展会逐个访问活跃店铺的公开 Orders 排序页更新监控池，再采集当天尚无有效快照的商品。自动发现且已离开前 20 的商品会停用，手动添加商品不会自动停用。扩展心跳仅表示电脑在线；页面验证仍由用户处理。扩展需使用包含 worker identity 与 claim token 的新版本；旧版本不会领取受控队列。

如果页面要求验证，任务会暂停并保留当前标签页，同时发送 Chrome 系统通知。人工正常完成验证后，扩展会自动继续；也可以在监控台点击“验证完成，继续任务”。系统不会识别或绕过验证码，也不会读取 Cookie、密码或浏览器凭据。

“重新检查今日任务”会幂等地创建或刷新当日队列，但仍保持待启动；选择在线电脑并点击“开始采集”后才会派发。`remove-browser-collector-task.bat` 只删除 Windows 计划任务，不会删除扩展配置或历史监控数据。

应用容器启动时会先执行 `alembic upgrade head`。PostgreSQL 数据保存在 `postgres_data` volume。

## 本地开发

Python 3.12+：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
$env:DATABASE_URL = "sqlite:///./monitor.db"
.\.venv\Scripts\alembic.exe upgrade head
.\.venv\Scripts\uvicorn.exe app.main:app --reload
```

SQLite 仅用于本地开发和测试；正式 Compose 环境使用 PostgreSQL。

运行测试：

```powershell
.\.venv\Scripts\python.exe -m pytest
```

## 主要 API

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/api/stores` | 添加同行店铺 |
| `PATCH` | `/api/stores/{id}` | 更新店铺或监控状态 |
| `POST` | `/api/stores/{id}/discover` | 从公开店铺页发现商品链接 |
| `GET` | `/api/stores/{id}/discovery-snapshots` | 查询店铺发现历史与失败原因 |
| `GET` | `/api/stores/{id}/top-products` | 查询昨日 IXSPY 日增量 Top 20 |
| `POST` | `/api/products` | 添加商品 URL/ID |
| `PATCH` | `/api/products/{id}` | 更新商品或监控状态 |
| `POST` | `/api/products/{id}/collect` | 手动采集单商品 |
| `POST` | `/api/jobs/collect-now` | 手动采集全部活跃商品 |
| `POST` | `/api/collection/browser-extension` | 接收 Chrome Extension 用户主动采集结果 |
| `POST` | `/api/collection/manual` | 接收手动录入的公开数据 |
| `GET` | `/api/collection/status/today` | 查询今日自动/手动采集状态 |
| `GET` | `/api/collection/pending` | 查询自动失败、待人工补采商品 |
| `GET` | `/api/browser-collection/runs/today` | 查询今日浏览器任务与心跳 |
| `POST` | `/api/browser-collection/runs/ensure` | 幂等创建或恢复今日任务 |
| `POST` | `/api/browser-collection/items/claim` | 扩展领取下一条顺序任务 |
| `GET` | `/api/products/{id}/snapshots` | 查询原始快照历史 |
| `GET` | `/api/products/{id}/daily-metrics` | 查询商品 IXSPY 直接日增量 |
| `GET` | `/api/stores/{id}/daily-metrics` | 查询店铺监控范围 IXSPY 日增量汇总 |

## 日增量规则

扩展在正常打开的商品页选择 IXSPY「增量」图表，读取截至目标日之前最近 7 个日期的可见提示值。只有图表实际显示的日期和值会保存；显示的 0 保留为 0，没有读到的日期保持缺失。

累计售出、`sold/orders` 总量和相邻快照差值都不参与日销量计算。历史 Snapshot 与旧估算指标保留在数据库中，但当前监控台和日指标 API 仅展示 IXSPY 直接日增量。

只有最近 7 个日期都读到 IXSPY 日增量时才显示完整 7 日合计；覆盖不足时显示实际覆盖数，不用累计值补齐或推算。

## Collector 扩展

业务层只依赖 `Collector` 协议。MVP 默认实现为 `HTTPCollector`，优先读取页面里的 JSON/JSON-LD。`PlaywrightCollector` 目前是明确禁用的扩展位；如后续启用，也应仅渲染无需登录的公开页面，并继续遵守平台规则。

现有本机 `aliexpress-selection-workflow` skill 可在后续“关键词发现商品”阶段产生候选商品链接，但不参与本 MVP 的快照采集和销量计算核心链路。
# 云端数据与登录

监控台现在支持把本地 PostgreSQL 数据迁移到 Render、Supabase 或其他 PostgreSQL 云数据库，并通过账号登录保护 Dashboard、API 和 Chrome 扩展。

## 首次配置

复制 `.env.example` 为 `.env`，至少设置：

```env
AUTH_REQUIRED=true
AUTH_SESSION_SECRET=一段足够长的随机字符串
AUTH_ADMIN_USERNAME=你的管理员账号
AUTH_ADMIN_PASSWORD=你的管理员密码
AUTH_COOKIE_SECURE=false
AUTH_COOKIE_SAMESITE=lax
```

首次启动时，如果数据库中还没有用户，Backend 会用这两个管理员环境变量创建第一个账号。密码只保存为 scrypt 哈希；启动完成后可以从 `.env` 删除 `AUTH_ADMIN_PASSWORD`，避免明文长期留在环境文件中。

如果 Backend 已经启动但还没有管理员，也可以双击项目根目录的 `setup-auth.bat`。它会构建最新 Backend、应用迁移、隐藏输入密码、创建管理员并重启服务，不会把密码写进仓库。

Render 生产环境应设置 `AUTH_COOKIE_SECURE=true`、`AUTH_COOKIE_SAMESITE=none`，并为 `AUTH_SESSION_SECRET` 设置新的随机值。`render.yaml` 已声明这些变量，部署时在 Render Environment 中填写 `sync: false` 的项目。

## 本地数据迁移到云端

从 Render PostgreSQL 的 External Database URL 或其他云数据库控制台复制完整 PostgreSQL 连接串，然后在 PowerShell 中执行：

```powershell
.\scripts\sync-local-to-cloud.ps1 -CloudDatabaseUrl "postgresql://用户名:密码@主机:5432/数据库名?sslmode=require" -ConfirmOverwrite
```

这是覆盖式迁移，会用本地监控数据替换云端同名表；执行前应确认云端没有需要保留的独立数据。脚本不会迁移旧登录会话，迁移完成后重新登录即可。执行前先启动本地数据库：`docker compose up -d postgres`。

迁移后，把 Render Backend 的 `DATABASE_URL` 设置为同一个云端连接串，重新部署 Backend；所有电脑访问同一个 Hosted Frontend 和 Backend，就会看到同一套店铺、商品、快照与销量历史。

## 多电脑使用

1. 在云端 Backend 和 Frontend 使用同一个账号登录。
2. 每台需要采集的电脑安装并重新加载 `extension/` 扩展。
3. 扩展的 `extension/config.js` 指向云端 Backend URL，而不是 `127.0.0.1`。
4. 在扩展弹窗登录同一个账号；AliExpress 和店小秘的登录状态仍属于各自电脑的 Chrome，不会上传到监控系统。

## 选品工作台候选联动

选品工作台通过 `POST /api/integrations/selection/candidates` 导入 AliExpress 候选；同批次重试使用相同 `batch_id` 幂等去重，保留候选来源。Amazon 条目留在选品工作台，不会导入监控台。运行新版本前先应用 Alembic migration。

在选品资格页 `/selection-qualification` 配置“七日总增量”和“有增量天数”两个阈值。默认未配置；两个值都填写后，监控台才会在完整七天 IXSPY 直接日增量满足条件时创建一个店小秘 handoff。完整七天中的 0 会计入；缺失日期、采集失败或非 IXSPY 数据不会按 0 补齐。新候选进入每日浏览器队列的独立商品阶段，不会混入竞品店铺发现任务。要立即补入当日队列，可在该页面点击“加入今日候选采集队列”；随后仍需选择在线采集电脑并点击“开始采集”。

服务端配置 `.env` 中的 `SELECTION_SERVICE_TOKEN` 和 `DIANXIAOMI_SERVICE_TOKEN`。前者只给选品工作台导入使用；后者必须与本机店小秘助手 `.env` 中的值相同，供 Docker 私有网络上的 worker API 验证。不要将令牌放入前端或提交到 Git。候选页面展示每日覆盖、增量、资格状态和店小秘任务；失败任务需人工检查后重试。

本地助手目前还没有完成店小秘真实页面字段映射和单商品验收。启用 handoff 后，如果助手无法确认真实页面，会暂停任务并等待人工处理；模拟流程通过不代表真实采集、编辑或保存到“待发布”已验收。流程终点为“待发布”，不会执行正式发布。
