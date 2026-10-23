# 店铺身份延后审核与队列安全实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**目标：** 店铺身份不明确时只跳过当前店铺并继续队列；人工审核只记录身份或拒绝，不自动补采；真正的技术失败停止后续派发。

**架构：** 复用 `BrowserCollectionItem.SKIPPED`、现有身份审核 URL/证据 JSON 与 `StoreUrlIdentity`，不增加状态或数据库迁移。后端统一处理扩展预检和结果入库中的身份错误，扩展释放当前页面后继续领取，监控台提供独立审核视图及显式单项重排。

**技术栈：** Python、FastAPI、SQLAlchemy、pytest；Chrome MV3 扩展 JavaScript、Node.js `node:test`；现有 Jinja 模板与 dashboard JavaScript/CSS。

**设计稿：** `docs/superpowers/specs/2026-10-20-store-identity-deferred-queue-design.md`

## 全局约束

- 身份待复核项使用 `status=SKIPPED`、`error_type=store_identity_deferred` 和现有 `identity_review_url`、`identity_review_evidence` 字段；除非数据库约束无法表达，不增加迁移。
- 未经人工确认不得写入商品、销量或店铺快照，不推进续页游标，不新增 URL 身份记录。
- 人工确认只为当前监控店铺登记精确 URL 身份并保持本轮跳过；只有显式单项重排才将其重新设为待处理。
- 身份判断不得仅依赖路由 ID 或店铺名；不得自动改写监控店铺规范 ID/URL，也不得从缺失证据推造信息。
- AliExpress 登录/验证码仍进入 `NEEDS_VERIFICATION` 并暂停；身份待审核与平台验证不得共用暂停处理。
- 店铺发现阶段同一队列最多有一个 `STORE` 项处于 `RUNNING`；产品预加载行为不变。
- 已手动暂停或因故障暂停的队列，不得因身份确认、心跳、扩展重载或部署而自动恢复。
- 当前生产队列 #30 保持暂停；本计划不重排生产任务、不重启服务、不部署、不代替用户完成真实页面验证。

## 评审重点

- 后端结果入库才发现 URL/身份冲突时，不能把它记成普通扩展失败，也不能留下部分商品/快照；由任务 2 的 API 测试覆盖。
- 过期 claim、并发领取、重复审核请求不能误操作其他店铺或重启队列；由任务 1、2 的并发与幂等测试覆盖。
- 平台挑战必须仍暂停并保留验证页，身份待审核则必须释放页并继续；由任务 3 的扩展测试分别覆盖。
- 普通技术失败最终停止派发，但 `PARTIAL` 不被误判为技术失败；由任务 1 的失败路径测试覆盖。
- 人工确认期间如果队列被手动暂停，确认不能清除暂停或自动重排；由任务 2 的审核状态测试覆盖。

---

### 任务 1：后端队列状态与派发保护

**文件：**
- 修改：`app/services/browser_collection.py`
- 测试：`tests/test_browser_collection.py`

**接口：**
- 使用现有 `record_store_identity_review(db, item, page_url, error_type, error_message, evidence, *, worker_id, claim_token) -> BrowserCollectionRun`。
- 保持现有 `claim_next_item(db, worker_id) -> tuple[BrowserCollectionRun | None, BrowserCollectionItem | None]` 与 `fail_item(db, item, error_type, error_message) -> BrowserCollectionRun` 接口。
- 身份决策标记统一存放在 `identity_review_evidence["identity_review_decision"]`，值为 `pending`、`confirmed` 或 `rejected`。

- [x] **步骤 1：先添加身份延后的失败测试**

新增并断言：`test_store_identity_review_skips_only_current_store_and_claims_next`、`test_store_identity_review_preserves_evidence_and_clears_claim`、`test_store_identity_review_does_not_mutate_another_legacy_claim`。两个正常待采店铺时，延后第一项后下一次 claim 返回第二家店；若另有旧版本遗留的 RUNNING claim，延后当前项仍成功且只清除当前 claim，不更改另一项，等待另一项终结后才领取下一家。

- [x] **步骤 2：运行新测试并确认其因旧行为失败**

运行：`pytest tests/test_browser_collection.py -k "store_identity_review_skips_only_current_store or store_identity_review_preserves_evidence" -q`

预期：失败，因为现有实现会把当前项和整队设为 `NEEDS_VERIFICATION`。

- [x] **步骤 3：把 `record_store_identity_review` 改为单项延后终态**

原子保存 URL 与证据，加入 `identity_review_decision="pending"`，将当前项设为 `SKIPPED/store_identity_deferred`，记录完成时间并清除当前 claim/lease；移除“存在另一 RUNNING 项就拒绝身份延后”的旧限制，但不改写另一项。调用 `_advance_run` 让队列按原派发状态前进；若队列已手动暂停，不得清除其暂停原因或启用派发。

- [x] **步骤 4：添加单店铺在途保护测试**

新增 `test_store_discovery_claim_allows_only_one_running_store_per_run`：同一 worker 首次 claim 得到店铺 A；A 仍为 `RUNNING` 时第二次 claim 返回无任务；A 被完成或身份延后后，下一次 claim 才可得到店铺 B。另断言 PRODUCT 阶段原有预加载/领取行为不变。

- [x] **步骤 5：在 `claim_next_item` 实施单个 STORE 在途限制**

在选取下一个 `STORE/PENDING` 项之前检查同一 run 是否已有 `STORE/RUNNING` 项；有则不再领取。不得将该限制扩展到 PRODUCT 预加载。

- [x] **步骤 6：添加终态技术失败暂停测试**

新增 `test_terminal_collection_failure_pauses_dispatch_but_partial_does_not`：可重试错误达到终次且仍有后续工作时，item 为 `FAILED` 且 run 为 `PAUSED`、`paused_reason="collection_failure"`、`dispatch_enabled=False`；身份延后和 `PARTIAL` 不触发此暂停。已手动暂停时保留原暂停原因；没有剩余工作时按现有逻辑结束为部分完成，不制造空队列暂停。

- [x] **步骤 7：实施终态失败停止派发**

在现有 `fail_item` 重试耗尽并准备终结为 `FAILED` 时，如仍有待派发工作则暂停派发；保留当前既有重试次数与 `page_not_ready` 重试策略。没有剩余工作时正常结算；`finish_item(..., "PARTIAL")` 不暂停。

- [x] **步骤 8：运行后端队列定向测试**

运行：`pytest tests/test_browser_collection.py -q`

预期：全部通过，包括既有手动暂停、平台验证和取消队列测试。

### 任务 2：结果入库身份错误、审核与手动重排

**文件：**
- 修改：`app/services/monitored_store_collection.py`
- 修改：`app/services/browser_collection.py`
- 修改：`app/api/router.py`
- 修改：`app/api/schemas.py`
- 测试：`tests/test_browser_collection.py`

**接口：**
- 新增 `StoreIdentityValidationError(ValueError)`，携带 `page_url: str`、`identity_error_type: str`、`evidence: dict` 和安全展示用 `message: str`；仅身份专属校验抛出该异常。
- 保持现有 `confirm_store_identity_review(db, item_id) -> BrowserCollectionRun`、`reject_store_identity_review(db, item_id) -> BrowserCollectionRun`。
- 保持现有 `POST /api/browser-collection/items/{item_id}/retry` 作为显式重排入口；服务层仅接受审核已确认的身份延后项。
- `BrowserCollectionItemRead` 新增 `identity_review_decision: str | None`，由现有证据 JSON 序列化，不增加数据库列。
- `identity_review_pending` 在 `SKIPPED/store_identity_deferred` 且 decision 为 `pending` 时为 true；确认或拒绝后为 false。

- [x] **步骤 1：添加入库阶段身份冲突测试**

新增 `test_store_discovery_identity_validation_becomes_deferred_without_snapshot`：构造有效 claim、实际 Orders URL 与目标身份冲突的结果，断言 API 返回该 run、任务变成 `SKIPPED/store_identity_deferred`，完整保存页面身份证据，且无 `StoreSnapshot`、商品销量写入或续页游标推进。

- [x] **步骤 2：运行测试确认当前错误路径**

运行：`pytest tests/test_browser_collection.py -k store_discovery_identity_validation_becomes_deferred -q`

预期：失败，因为现有 endpoint 将所有 `ValueError` 返回通用 409。

- [x] **步骤 3：为身份专属结果校验增加类型化异常**

仅在 `_validated_orders_url` 的路由 ID/身份不匹配和保存结果中明确的店铺身份冲突处抛 `StoreIdentityValidationError`；普通 URL 格式、排序、商品数据错误继续使用普通 `ValueError`。证据仅取自本次 payload 与实际页面 URL，不推造标题或 ID。

- [x] **步骤 4：让 store-discovery endpoint 将该异常转换成身份延后**

在 `browser_collection_store_discovery` 捕获 `StoreIdentityValidationError` 后先回滚全部业务写入，再用原 claim 重新验证并调用 `record_store_identity_review`；返回序列化 run。普通 `ValueError` 仍按 409 返回。

- [x] **步骤 5：添加确认、拒绝和幂等测试**

新增 `test_identity_confirm_records_alias_without_resuming_or_requeueing`、`test_identity_reject_records_decision_without_alias_or_pausing_queue`、`test_identity_review_decisions_are_idempotent_and_conflicts_stay_rejected`。确认只创建同店铺精确 URL 身份、保留 `SKIPPED` 和 `confirmed`；拒绝保留 `SKIPPED/rejected` 且不写 alias；重复提交结果稳定；跨店铺冲突仍拒绝。

- [x] **步骤 6：改造审核服务只写身份决策**

`_require_current_identity_review` 验证身份延后项和 `pending` 决策，不再要求 run 为 `NEEDS_VERIFICATION`。确认/拒绝不得要求采集 worker 在线；不得改写 run 状态、暂停原因、派发开关或 item 的 `SKIPPED` 状态。即使审核期间队列被手动暂停，也只写审核决策、不清除暂停。确认保留现有 URL 冲突与店铺归属校验。

- [x] **步骤 7：添加单项重排边界测试**

新增 `test_only_confirmed_identity_deferred_item_can_be_requeued`：pending、rejected、普通取消 SKIPPED 均返回 409；confirmed 项可通过现有 retry endpoint 单独回到 `PENDING`，保留审核证据，且该请求本身不打开或自动接管浏览器任务。

- [x] **步骤 8：实施受限的显式重排**

扩展 `retry_item` 仅将 `SKIPPED/store_identity_deferred` 且 decision 为 `confirmed` 的店铺项纳入可重排状态；沿用现有单项重置逻辑，不重置其他任务、不启用 dispatch，不清除证据。更新 item schema/serializer 暴露审核决策，并按 pending decision 计算 `identity_review_pending`。

- [x] **步骤 9：运行后端 API 与身份测试**

运行：`pytest tests/test_browser_collection.py tests/test_store_identity.py -q`

预期：全部通过；普通错误仍保持错误语义，身份错误只进入延后审核。

### 任务 3：扩展释放身份待审页面并继续队列

**文件：**
- 修改：`extension/background.js`
- 测试：`tests/extension_background_worker.test.cjs`

**接口：**
- 继续使用 `POST /api/browser-collection/items/{id}/identity-review` 上报预检身份问题。
- 使用现有 `releaseAutomationTab(tabId)` 释放当前任务页，并复用 `pollAutomation()` 获取下一项；不得调用 challenge endpoint 展示身份审核页面。

- [x] **步骤 1：添加扩展跳过并继续的失败测试**

新增 `test_identity_review_defers_item_releases_store_tab_and_claims_next`：模拟身份待审响应，断言只调用 identity-review endpoint、不调用 challenge endpoint；当前任务本地状态及 identity pause 标记被清除，任务页释放到 idle 复用池，后续 claim 被调用。

- [x] **步骤 2：运行新扩展测试确认旧行为**

运行：`node --test --test-name-pattern="identity_review_defers_item_releases_store_tab_and_claims_next" tests/extension_background_worker.test.cjs`

预期：失败，因为现有行为保留页面并设置 `identityReviewPaused`。

- [x] **步骤 3：把身份结果处理改为延后释放**

将 `pauseForStoreIdentityReview` 改为 `deferStoreIdentityReview` 单项延后处理：写入后清理身份暂停标记和通知状态，释放当前店铺页到 idle 复用池；不显示挑战验证提示、不等待人工审核返回。更新 `processAutomationTabInternal` 与异常分支对该流程的调用。

- [x] **步骤 4：添加结果入库延后及平台挑战隔离测试**

新增/更新测试，断言后端 store-discovery 返回身份延后终态后扩展关闭/释放页面并继续；AliExpress challenge 仍走原 `pauseForAliExpressChallenge` 并保持人工页面。

- [x] **步骤 5：运行扩展队列与身份测试**

运行：`node --test tests/extension_background_worker.test.cjs`

预期：全部通过，现有前台采集、页签恢复、worker claim 与平台挑战行为不回归。

### 任务 4：监控台独立审核视图与显式重排

**文件：**
- 修改：`app/templates/dashboard.html`
- 修改：`app/static/dashboard.js`
- 修改：`app/static/dashboard.css`
- 测试：`tests/test_dashboard.py`

**接口：**
- 复用现有确认/拒绝 API 和 `/api/browser-collection/items/{id}/retry`。
- 审核决策从 item 的 `identity_review_decision` 字段读取。

- [x] **步骤 1：添加监控台筛选与操作失败测试**

新增 `test_identity_deferred_items_have_separate_filter_and_decision_actions`：筛选项独立显示身份延后记录；pending 显示“确定无误/错误”，confirmed 显示“已确认 URL 身份/重新排队”，rejected 显示“已拒绝”；身份延后不计入普通失败数。

- [x] **步骤 2：运行 dashboard 测试确认缺失行为**

运行：`pytest tests/test_dashboard.py -k identity_deferred_items_have_separate_filter -q`

预期：失败，因为当前 UI 只把 NEEDS_VERIFICATION 身份任务作为审核项。

- [x] **步骤 3：增加独立身份审核筛选及计数**

在 `dashboard.html` 增加“身份审核”筛选项，并调整 `.browser-filter-grid` 的桌面与窄屏列数/边框规则；在 `dashboard.js` 展示待审及已处理记录，计数覆盖全部身份审核记录，以便确认后仍可手动重排；普通失败计数排除所有身份延后项。

- [x] **步骤 4：实现决策按钮和反馈**

待审项展示“确定无误/错误”；确认后保留在身份记录中并显示显式“重新排队”按钮；拒绝后展示已拒绝且不提供身份重排按钮。确认/拒绝通知不得声称队列继续/暂停，也不得调用 run resume。

- [x] **步骤 5：运行监控台与模板测试**

运行：`pytest tests/test_dashboard.py -q`

预期：全部通过，其他队列状态筛选与现有确认页面不回归。

### 任务 5：跨组件回归与交付核验

**文件：**
- 验证：`app/services/browser_collection.py`、`app/services/monitored_store_collection.py`、`app/api/router.py`、`app/api/schemas.py`、`extension/background.js`、`app/templates/dashboard.html`、`app/static/dashboard.js`、`app/static/dashboard.css`
- 验证：`tests/test_browser_collection.py`、`tests/test_store_identity.py`、`tests/test_dashboard.py`、`tests/extension_background_worker.test.cjs`

- [x] **步骤 1：运行全部相关后端测试**

运行：`pytest tests/test_browser_collection.py tests/test_store_identity.py tests/test_dashboard.py -q`

预期：全部通过。

- [x] **步骤 2：运行全部扩展 worker 测试**

运行：`node --test tests/extension_background_worker.test.cjs`

预期：全部通过。

- [x] **步骤 3：核查实现没有扩大到无关状态或生产队列**

检查 diff 与测试结果：没有迁移、没有更改规范店铺 ID/URL、没有自动接受身份、没有自动恢复/重排 #30、没有触碰 IXSPY 商品排序/池逻辑。

- [x] **步骤 4：向用户汇报实现和验证边界**

分别列明代码测试、扩展重载、backend 部署、生产队列状态和真实人工验证的实际结果。不得把单元测试或扩展版本号表述为第二台电脑的真实采集验收。
