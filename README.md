# AliExpress 竞品监控 MVP

用于选品研究的 AliExpress 公开数据监控系统。系统按日保存商品页面快照，并通过相邻自然日公开累计 `sold/orders` 的差值估算商品日销量。店铺汇总只覆盖已添加并保持活跃的监控商品，不代表店铺真实后台订单量或全店销量。

## MVP 能力

- 手动添加同行店铺和商品 URL/ID
- 输入店铺链接后，从公开店铺页发现商品并自动建立监控列表
- HTTP 优先采集公开商品页，保存原始响应与结构化字段
- 解析失败照常保存 Snapshot，不覆盖历史记录
- 按 `Asia/Shanghai` 自然日估算商品销量并汇总店铺监控范围
- APScheduler 每日采集，以及单商品/全量手动采集
- FastAPI JSON API、OpenAPI 文档和轻量中文 Dashboard
- AUTO FIRST + BROWSER FALLBACK：HTTP 失败后由专用 Chrome 顺序采集，验证页只允许人工正常通过
- PostgreSQL + Alembic + Docker Compose 部署

系统不实现登录、验证码绕过、浏览器指纹规避、代理轮换或其他平台安全机制规避。若公开页面拒绝访问或结构无法识别，会保存失败快照供排查。

## Windows 本地部署

日常使用不依赖 Codex Preview。Windows 安装并启动 Docker Desktop 后，双击项目目录中的 `start.bat`，脚本会启动 PostgreSQL、Backend、Frontend 和 Scheduler，等待健康检查通过后自动打开：

<http://127.0.0.1:3000>

停止使用时双击 `stop.bat`；需要重新构建或重启时双击 `restart.bat`。PostgreSQL 数据保存在 Docker volume `postgres_data`，执行 `docker compose down` 不会删除该 volume。

项目目录中还提供 `AliExpress Monitor.url`，可以右键发送到桌面作为网页快捷方式。网页快捷方式要求服务已经运行；希望双击时自动启动 Docker 的场景，请为 `open-monitor.bat` 创建桌面快捷方式。

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
- API 文档: <http://localhost:8000/docs>
- 健康检查: <http://localhost:8000/health>

## Chrome Extension 手动补采

扩展目录为 `extension/`。在 Chrome 打开 `chrome://extensions`，开启“开发者模式”，选择“加载已解压的扩展程序”并选中该目录。然后从监控台商品详情页点击“在 Chrome 打开”，在商品页点击扩展图标中的“采集当前商品”。

扩展只读取当前商品页的公开可见信息，并通过 `POST /api/collection/browser-extension` 写入新的 `ProductSnapshot(source=CHROME_EXTENSION)`；它不会读取 Cookie、绕过验证码或后台自动监控页面。自动采集失败的商品可在 `/collection/pending` 查看并补采。

## Hosted Chrome Extension

扩展的 API 地址在 [extension/config.js](extension/config.js) 中配置。将其中的 `http://127.0.0.1:8000` 改成同一个托管 Backend origin，并在 `extension/manifest.json` 的 `host_permissions` 中加入该 Backend 的精确 origin，例如：

```json
"https://monitor-api.example.com/*"
```

不要使用 `https://*/*` 放宽权限。修改后在 `chrome://extensions` 重新加载扩展。扩展仍然需要在用户自己的 Chrome 配置中正常登录 AliExpress；它不会把 Cookie 或登录凭据上传到 Backend。

## 每日浏览器自动采集

双击 `install-browser-collector.bat` 会创建 Windows 计划任务 `AliExpress Monitor Daily Collection`，每天北京时间 `02:00` 调用 `start-browser-collector.bat`。如果计划时间电脑未开机，Windows 会在当前用户下次登录后尽快补跑。专用浏览器配置保存在 `%LOCALAPPDATA%\AliExpressMonitor\ChromeProfile`，与日常 Chrome 配置隔离。

首次安装后需在脚本打开的专用 Chrome 中完成两件事：从 `extension/` 加载未打包扩展，以及正常登录 AliExpress。之后每日流程会先刷新各活跃店铺的公开订单排序页并更新前 20 监控池，再逐个采集当天尚无有效快照的商品。自动发现且已离开前 20 的商品会停用，手动添加商品不会自动停用。

如果页面要求验证，任务会暂停并保留当前标签页，同时发送 Chrome 系统通知。人工正常完成验证后，扩展会自动继续；也可以在监控台点击“验证完成，继续任务”。系统不会识别或绕过验证码，也不会读取 Cookie、密码或浏览器凭据。

可在监控台点击“立即运行今日任务”创建或恢复当日幂等队列。`remove-browser-collector-task.bat` 只删除 Windows 计划任务，不会删除专用 Chrome 配置或历史监控数据。

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
| `GET` | `/api/stores/{id}/top-products` | 查询昨日估算销量 Top 20 |
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
| `GET` | `/api/products/{id}/daily-metrics` | 查询商品日销量估算 |
| `GET` | `/api/stores/{id}/daily-metrics` | 查询店铺监控范围估算 |

## 估算规则

每天使用该自然日最后一条 Snapshot 作为代表值，与后一自然日代表值比较，差值归属于区间起始日。例如 9 月 19 日到 20 日的公开累计量差值记为 9 月 19 日估算销量。以下情况不会强行填充销量：

- 没有前一日基线
- 两个快照日期不连续
- 任一代表快照解析失败
- 累计 sold/orders 缺失
- 累计值下降（可能是页面口径、商品变体或数据重置）

API 使用 `estimated_sales`、`estimate_type=estimated`、`data_basis=public_observable_data`；店铺指标额外返回 `estimate_scope=monitored_products_only`。

首次发现商品时只能得到候选商品链接，不能从单次公开数据判断昨日销量。系统会明确显示“等待日销量基线”；连续两天采集完成后，店铺详情页才会按估算销量显示昨日 Top 20。候选商品的公开累计销量只用于发现阶段排序，不会被表述成日销量。

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
