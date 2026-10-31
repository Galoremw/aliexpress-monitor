# 店小秘共用监控台 Chrome 与采集机归属实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让店小秘任务只由监控台当前选中的采集电脑领取，并在该电脑已打开的监控台 Chrome 窗口中处理商品页。

**Architecture:** 复用 `BrowserCollectionControl.selected_worker_id` 和现有稳定扩展 worker UUID；后端在店小秘队列的所有领取入口执行归属校验，阻止非选中电脑和不兼容 worker 抢任务。扩展从 worker 监控台标签取得窗口 ID，把一个可复用的 AliExpress 商品标签放入同一窗口；店小秘状态面板显示选中电脑与 owner 等待状态。

**Tech Stack:** FastAPI、SQLAlchemy、PostgreSQL、Alembic（预期不需迁移）、Chrome Extension Manifest V3、Jinja、原生 JavaScript、pytest、Node.js test runner。

**Spec:** `docs/superpowers/specs/2026-10-30-dianxiaomi-shared-chrome-collector-ownership.md`

## Global Constraints

- 店小秘与店铺采集共用唯一的监控台“采集电脑”选择器。
- 只有后端当前选中的在线 worker 可以领取新的店小秘任务；页面加载或 worker 上线不得自动选择电脑或启动队列。
- 单次只处理一个商品；AliExpress / 店小秘登录、验证与协议确认由用户本人完成。
- AliExpress 商品页必须与发起任务的监控台标签位于同一个 Chrome `windowId`；不得为店小秘商品任务调用 `chrome.windows.create`。
- 不清空、重排或自动恢复现有队列；owner 切换时旧任务可结束，新 worker 在旧任务完成或租约过期前不得领取。
- 不改变商品身份判定、店小秘助手成功规则、店铺采集逻辑或用户角色权限。
- 预期不新增数据库迁移；如果实现发现现有表结构不足，停止并更新设计后再继续。
- NAS 部署和两台电脑扩展重载是独立发布步骤；不自动恢复队列或启动真实采集。

## Review Focus

- 无电脑被选中：所有 Chrome 与服务 token 领取入口都必须拒绝领取。
- 选中电脑离线：任务保持排队，不得回退到另一台在线电脑；由后端心跳时间验证。
- Galore 与 QX 同时在线：非 owner 的 claim 和任务创建回报不得领取/覆盖当前任务。
- owner 切换时旧 worker 尚有有效任务：旧任务可完成或自然过期，新 worker 不并行领取；旧请求不能覆盖过期后重领的状态。
- 监控台标签关闭或不在 worker 模式：扩展不得另开窗口后继续采集，队列任务也不得被错误领取。

---

### Task 1: 后端为店小秘领取和任务回报强制采集机归属

**Files:**
- Modify: `app/services/dianxiaomi.py`
- Modify: `app/api/router.py`
- Modify: `tests/test_dianxiaomi.py`

**Interfaces:**
- Consumes: `BrowserCollectionControl.selected_worker_id`、`BrowserCollectionWorker.last_heartbeat_at`、现有 `DianxiaomiHandoff.worker_id`。
- Produces: `claim_handoff_batch(db: Session, worker_id: str, *, limit: int = 20, resume_confirmed: bool = False) -> list[DianxiaomiHandoff]` 在领取前核验选中 worker、在线状态、队列暂停状态及其他 worker 的有效活动任务；各状态更新入口继续以 `DianxiaomiHandoff.worker_id` 栅栏。

- [x] **Step 1: 为 owner 规则添加服务/API 回归测试**

在 `tests/test_dianxiaomi.py` 添加 `test_dianxiaomi_claim_requires_selected_online_worker`、`test_dianxiaomi_claim_waits_for_other_workers_active_handoff`、`test_dianxiaomi_status_rejects_stale_owner_after_requeue`、`test_dianxiaomi_docker_claim_obeys_selected_owner`。断言未选中/非 owner/离线/暂停时 claim 不返回任务；所选在线 worker 可领取；旧任务仍有效时新 owner 不并发领取；租约到期并退回 QUEUED 后旧 worker 的状态回报得到 409。同步修改现有测试 fixture，为既有正常领取场景创建并选择在线 worker，避免旧测试依赖无 owner 的行为。

- [x] **Step 2: 运行店小秘定向测试并确认新场景失败**

运行：`pytest tests/test_dianxiaomi.py -q`

预期：新建的 owner/在线/跨 worker 场景失败，原有店小秘行为测试维持其当前结果。

- [x] **Step 3: 在 `app/services/dianxiaomi.py` 实现选中 owner 与活动任务检查**

新增单一内部校验函数，使用浏览器采集已有在线阈值两分钟；对其他 worker 的 `CLAIMED`、`OPENED`、`FILLED`、`COLLECTING` 状态，仅在租约仍有效时阻止新 owner 领取。保留现有过期租约重新排队行为。

- [x] **Step 4: 在 `app/api/router.py` 覆盖每个领取与状态入口**

统一检查 `claim-next`、`claim-batch`、单条 `claim` 及 service-token/Docker claim。扩展 heartbeat 必须带稳定 `worker_id`。普通状态更新要求请求 worker 与任务当前 owner 匹配；owner 切换后允许旧 owner 完成自己仍有效的任务，但不允许旧请求改写已过期并重新领取的任务。service-token 路径不得绕过选中 owner。

- [x] **Step 5: 运行店小秘定向测试**

运行：`pytest tests/test_dianxiaomi.py -q`

预期：owner、在线、暂停、handoff/lease 与旧状态栅栏场景全部通过。

### Task 2: 扩展使用稳定电脑 ID，并在监控台同一窗口处理商品页

**Files:**
- Modify: `extension/background.js`
- Modify: `extension/dashboard_bridge.js`
- Modify: `tests/extension_background_worker.test.cjs`
- Modify: `tests/extension_dashboard_bridge.test.cjs`

**Interfaces:**
- Consumes: `browserCollectionWorkerIdentity() -> { worker_id: string, worker_label: string }`、worker 模式 dashboard 的 `sender.tab.id` 与 `sender.tab.windowId`。
- Produces: `pollDianxiaomiHandoffs()` 使用稳定 worker UUID；`ensureDianxiaomiProductTab(url: string, dashboardWindowId: number) -> Promise<{ tab: chrome.tabs.Tab, windowId: number }>` 只在指定窗口创建或复用商品标签；缺少有效 dashboard window 时不 claim。

- [x] **Step 1: 添加扩展身份和同窗行为测试**

在 `tests/extension_background_worker.test.cjs` 添加 `testDianxiaomiUsesStableBrowserWorkerIdentity`、`testDianxiaomiReusesDashboardWindowForProductTab`、`testDianxiaomiDoesNotClaimWithoutWorkerDashboardContext`。断言 heartbeat/claim/status payload 使用已持久化的 worker UUID；商品 tab 的 `windowId` 等于 worker dashboard 的 `windowId` 且 fake Chrome 的新窗口计数不变；失去 dashboard 上下文时 claim API 调用次数为零。在 `tests/extension_dashboard_bridge.test.cjs` 添加 `testRemoteBrowserCollectionDashboardPublishesDianxiaomiWorkerContext`，断言 NAS `browser_collection=1` 页面声明 worker mode。tab ID 和 window ID 由 background 从 Chrome 提供的 `sender.tab` 读取并保存，content script 无法直接读取这两个 ID。

- [x] **Step 2: 运行扩展定向测试并确认新场景失败**

运行：`node --test tests/extension_background_worker.test.cjs tests/extension_dashboard_bridge.test.cjs`

预期：新身份与同窗测试失败，既有测试结果不因测试夹具变化而掩盖失败。

- [x] **Step 3: 在 `extension/dashboard_bridge.js` 标记 worker dashboard**

对 `browser_collection=1` worker 模式监控台标签发送 Dianxiaomi worker 标记；background 使用 Chrome `sender.tab.id/windowId` 保存来源上下文。非 worker dashboard 不启用店小秘队列 polling。

- [x] **Step 4: 在 `extension/background.js` 改用稳定 worker 身份并复用窗口**

店小秘 heartbeat、claim、status 调用统一复用 `browserCollectionWorkerIdentity()`。保存当前 worker dashboard 的 tab/window；`ensureDianxiaomiProductTab` 在同一窗口复用单个商品标签。若 dashboard 标签/window 已失效，则停止 claim，不调用 `chrome.windows.create`。

- [x] **Step 5: 运行扩展定向测试**

运行：`node --test tests/extension_background_worker.test.cjs tests/extension_dashboard_bridge.test.cjs`

预期：QX 与 Galore 使用不同 profile UUID；商品页复用 worker dashboard 窗口，且无 dashboard context 时不领任务。

### Task 3: 在店小秘面板显示选中电脑和领取阻塞状态

**Files:**
- Modify: `app/api/router.py`
- Modify: `app/static/dashboard.js`
- Modify: `app/templates/dashboard.html`
- Modify: `tests/test_dianxiaomi.py`
- Modify: `tests/test_dashboard.py`

**Interfaces:**
- Consumes: 后端已有 `BrowserCollectionControl` 与 `BrowserCollectionWorker` 登记数据。
- Produces: `/api/integrations/dianxiaomi/status` 返回 `selected_worker`、`worker_online`、当前活动任务 owner 和 owner handoff/waiting 状态；店小秘面板显示其名称、在线状态与等待原因，不新增第二个电脑选择器。

- [x] **Step 1: 添加状态序列化与面板呈现测试**

在 `tests/test_dianxiaomi.py` 添加 `test_dianxiaomi_status_reports_selected_worker_and_active_owner`，断言 status API 返回 selected worker label/online、active handoff owner 和等待状态。在 `tests/test_dashboard.py` 添加 `test_dianxiaomi_panel_shows_shared_collector_without_second_selector`，断言店小秘面板显示共享采集机状态，页面只保留日常采集的唯一电脑选择器。

- [x] **Step 2: 运行对应定向测试并确认新增断言失败**

运行：`pytest tests/test_dianxiaomi.py tests/test_dashboard.py -q`

预期：新增状态字段和 UI 断言失败。

- [x] **Step 3: 返回并渲染 owner 状态**

在 `/api/integrations/dianxiaomi/status` 从浏览器采集 worker 注册表计算选中 worker 在线状态，并返回活动 handoff 的 worker label 与等待状态。

- [x] **Step 4: 更新 dashboard 的店小秘状态文案**

在 `dashboard.html` 添加只读状态节点；`dashboard.js` 显示“当前采集电脑：名称 · 在线/离线”，并在 owner 切换或旧任务有效时提示等待。继续/暂停按钮语义不变。

- [x] **Step 5: 运行对应定向测试**

运行：`pytest tests/test_dianxiaomi.py tests/test_dashboard.py -q`

预期：状态 API、面板归属显示和既有队列控件测试通过。

### Task 4: 移除独立 Dianxiaomi Chrome 启动方式并更新使用说明

**Files:**
- Modify: `setup-dianxiaomi-worker.bat`
- Modify: `start-dianxiaomi-worker.bat`
- Modify: `scripts/start-local-dianxiaomi-worker.ps1`
- Modify: `extension/README.md`
- Modify: `README.md`

- [x] **Step 1: 更新启动脚本**

移除 `DianxiaomiProfile`、`--user-data-dir`、`--new-window`、关闭/终止 Chrome 进程和单独加载扩展的逻辑。脚本只提示使用已登录且安装两种扩展的监控台 Chrome；不得自行启动另一配置。

- [x] **Step 2: 更新运行说明**

说明 QX 与 Galore 分别在已安装扩展的现有 Chrome 打开 worker 模式监控台，使用同一个“采集电脑”选择器选定实际执行机器；身份验证仍由用户处理。

- [x] **Step 3: 检查文档与脚本差异**

运行：`git diff --check -- setup-dianxiaomi-worker.bat scripts/start-local-dianxiaomi-worker.ps1 extension/README.md README.md`

预期：无空白错误；新说明中不再要求创建隔离配置或单独安装店小秘助手。

### Task 5: 端到端发布前核对与 NAS 发布门槛

**Files:**
- Review: `docs/superpowers/specs/2026-10-30-dianxiaomi-shared-chrome-collector-ownership.md`
- Review: `docs/superpowers/plans/2026-10-30-dianxiaomi-shared-chrome-collector-ownership.md`
- Review: all Task 1–4 changed files

- [ ] **Step 1: 运行后端与扩展相关定向测试**

运行：`pytest tests/test_dianxiaomi.py tests/test_dashboard.py -q` 及 `node --test tests/extension_background_worker.test.cjs tests/extension_dashboard_bridge.test.cjs`。

预期：命令退出码为 0；无数据库迁移待应用。

- [x] **Step 2: 执行静态差异检查**

运行：`git diff --check`，并检查所有店小秘 claim API 均经过 owner 校验、商品标签创建不含 `chrome.windows.create`。

- [x] **Step 3: 汇总发布前行为边界**

确认 QX 选中时 Galore 被拒绝、当前商品页与 dashboard 同窗、无选中/离线时不领取、owner 切换不并发、现有队列状态未被清理或自动启动。

- [x] **Step 4: 停在 NAS 部署门槛**

先报告代码与本机验证结果；NAS backend 部署、QX/Galore 扩展复制/重载、恢复队列及真实商品页验收分别等待用户明确安排。部署不得自动恢复或启动店小秘队列。

## Plan Self-Review

- **Spec coverage:** owner 归属、claim/status fencing、活动任务 drain、同窗口标签、状态显示、旧启动脚本、兼容性、人工验证和队列保留均映射到具体任务。
- **Step scan:** 每个步骤限定一个测试、实现或文档动作，并给出文件与预期结果。
- **Type consistency:** worker identity、dashboard window、status response 和 queue service 接口在任务间保持一致。
- **Review Focus:** 五种关键失效条件均在 Task 1 或 Task 2 的测试步骤中覆盖。
- **Proportion:** 计划只包含该单一店小秘队列归属与窗口工作流，不扩展到店铺采集、身份审核或账号角色系统。
