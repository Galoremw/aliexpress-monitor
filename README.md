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
- AUTO FIRST + MANUAL FALLBACK：自动采集失败进入待补采队列，可用 Chrome Extension 用户主动补采
- PostgreSQL + Alembic + Docker Compose 部署

系统不实现登录、验证码绕过、浏览器指纹规避、代理轮换或其他平台安全机制规避。若公开页面拒绝访问或结构无法识别，会保存失败快照供排查。

## Windows 本地部署

日常使用不依赖 Codex Preview。Windows 安装并启动 Docker Desktop 后，双击项目目录中的 `start.bat`，脚本会启动 PostgreSQL、Backend、Frontend 和 Scheduler，等待健康检查通过后自动打开：

<http://127.0.0.1:3000>

停止使用时双击 `stop.bat`；需要重新构建或重启时双击 `restart.bat`。PostgreSQL 数据保存在 Docker volume `postgres_data`，执行 `docker compose down` 不会删除该 volume。

项目目录中还提供 `AliExpress Monitor.url`，可以右键发送到桌面作为网页快捷方式。网页快捷方式要求服务已经运行；希望双击时自动启动 Docker 的场景，请为 `open-monitor.bat` 创建桌面快捷方式。

## GitHub Pages Deployment

GitHub Pages 部署的是 `frontend/` 下的静态前端，当前配置连接使用者本机的 `http://127.0.0.1:8000` Backend。使用 Pages 前，必须在本机启动 Docker/Backend；否则页面会显示 Backend 连接失败。GitHub Pages 本身不运行数据库、Scheduler、Collector 或 Chrome Extension Backend。前端使用 Hash 路由。

GitHub Actions 文件为 `.github/workflows/deploy-pages.yml`，在 `main` 分支 push 或手动触发时执行 `npm ci`、`npm run build`、`npm test`，然后使用 GitHub Pages 官方 Actions 发布 `frontend/dist`。

### Environment Variables

`VITE_API_BASE_URL` 只能填写 API 地址，不能放 API key、数据库密码、Cookie、Token 或其他秘密。本次 Pages 构建使用 `http://127.0.0.1:8000`，它只对打开 Pages 的同一台电脑有效。生产 Backend 尚未部署时，不要把本地地址误认为公网 API。

### Backend Deployment

GitHub Pages 只能托管静态 Frontend。FastAPI、PostgreSQL、APScheduler 和 AliExpress Collector 仍需运行在本地 Docker 或另一个受控 Backend 环境中。当前本地入口仍然是 `http://127.0.0.1:3000`，Pages Demo 不会连接访问者电脑上的 `127.0.0.1:8000`。

### Architecture

```text
GitHub Pages -> static frontend demo only
Local Docker -> frontend proxy + FastAPI + PostgreSQL + scheduler
Chrome Extension -> local FastAPI browser-extension endpoint
```

固定地址：

- Frontend: <http://127.0.0.1:3000>
- Backend: <http://127.0.0.1:8000>
- API 文档: <http://127.0.0.1:8000/docs>

## Docker Compose 启动

```bash
docker compose up -d --build
```

启动后访问：

- Dashboard: <http://localhost:8000/>
- API 文档: <http://localhost:8000/docs>
- 健康检查: <http://localhost:8000/health>

## Chrome Extension 手动补采

扩展目录为 `extension/`。在 Chrome 打开 `chrome://extensions`，开启“开发者模式”，选择“加载已解压的扩展程序”并选中该目录。然后从监控台商品详情页点击“在 Chrome 打开”，在商品页点击扩展图标中的“采集当前商品”。

扩展只读取当前商品页的公开可见信息，并通过 `POST /api/collection/browser-extension` 写入新的 `ProductSnapshot(source=CHROME_EXTENSION)`；它不会读取 Cookie、绕过验证码或后台自动监控页面。自动采集失败的商品可在 `/collection/pending` 查看并补采。

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
