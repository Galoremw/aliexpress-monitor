# Held Exclusive Browser Collection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make daily browser collection wait in a held state until the user selects an online computer and explicitly starts the queue, while ensuring only that computer can claim work.

**Architecture:** Store the selected worker in a singleton control row, worker heartbeats in a registry table, and a dispatch-enabled gate on each daily run. Enforce ownership and claim fencing at the API/service boundary, then propagate stable Chrome-profile identity and claim tokens through the extension. Put selection, start/resume, status, and handoff controls in the existing daily browser-collection panel.

**Tech Stack:** FastAPI, SQLAlchemy, Alembic, PostgreSQL/SQLite tests, Jinja templates, static JavaScript/CSS, Chrome Extension Manifest V3, pytest, Node.js tests.

**Spec:** `docs/superpowers/specs/2026-10-10-exclusive-browser-collector-ownership.md`

## Global Constraints

- Applies only to the daily browser-collection queue; do not change Dianxiaomi or review queues.
- Scheduled and manual queue preparation do not release the held queue.
- Selecting a computer and starting collection are separate user actions.
- Only the selected computer may claim; do not add automatic failover or parallel collection.
- Existing queue items, snapshots, metrics, browser profiles, and collection history remain intact.
- Do not clear/reset queues, deploy, restart services, reload Chrome/extension, or start a live queue as part of implementation or tests.
- Preserve all preexisting work in the dirty shared checkout; edit and test only the scoped files below.
- Do not stage or commit the shared checkout; unrelated user changes are already present.

## Review Focus

- A legacy extension without worker identity must not claim while a run is held — Task 1: `test_held_run_rejects_legacy_and_unselected_claims`.
- Missing/offline selected workers must not start the run — Task 1 and Task 3: `test_start_requires_selected_online_worker`, `test_dashboard_disables_start_for_offline_worker`.
- A handoff must stop old claims, drain running work, and reject stale writes — Task 2: `test_handoff_drains_old_claim_and_rejects_stale_result`.
- Scheduler, `立即采集全部`, queue refresh, and page reload must not release the hold — Task 1 and Task 3: `test_scheduler_and_collect_all_leave_run_held`, `test_queue_preparation_does_not_dispatch_or_wake_worker`.
- Extension alarms/prefetch must not open collection tabs while held — Task 2: `test_held_worker_does_not_open_collection_window_or_item_tab`.

---

## Implementation Map

- `app/db/models.py` and the Alembic migration persist worker heartbeats, selected ownership, per-run dispatch state, and per-attempt claim fencing.
- `app/services/browser_collection.py`, `app/api/schemas.py`, and `app/api/router.py` enforce the gate and ownership for every browser claim/write while preserving current lease behavior.
- `extension/background.js` and `extension/dashboard_bridge.js` identify each Chrome profile, carry claim tokens, and avoid opening collection tabs before a permitted claim.
- `app/dashboard/router.py`, `app/templates/dashboard.html`, and `app/static/dashboard.js`/`dashboard.css` expose worker choice, queue state, and explicit start/resume without changing queue preparation semantics.
- Pytest and Node tests cover the service/API and extension contracts; README files document the operator flow. Deployment remains a separate, authorized activity.

### Task 1: Persist Worker Selection and Enforce the Queue Gate

**Files:**
- Create: `alembic/versions/20261010_0010_browser_collection_control.py`
- Modify: `app/db/models.py`
- Modify: `app/api/schemas.py`
- Modify: `app/api/router.py`
- Modify: `app/services/browser_collection.py`
- Test: `tests/test_browser_collection.py`
- Test: `tests/test_schema.py`
- Test: `tests/test_scheduler.py`

**Interfaces:**
- Add `BrowserCollectionControl(id=1, selected_worker_id, selected_worker_label, updated_at)` and `BrowserCollectionWorker(worker_id, label, extension_version, last_heartbeat_at)`.
- Add `BrowserCollectionRun.dispatch_enabled: bool`, default `False`; close it on pause, verification hold, handoff, and terminal completion. Add `BrowserCollectionItem.claimed_worker_id` and nullable per-attempt `claim_token`.
- Add `POST /api/browser-collection/control/worker` with `{worker_id, confirm_handoff}`; add `GET /api/browser-collection/control` returning the selected worker, known workers with online status, today's `dispatch_enabled`, and handoff-drain status.
- Add `POST /api/browser-collection/runs/start-today`; it ensures today's run if absent, then starts only when a worker is selected and online and no foreign/unknown item remains running.
- Change claim input to `{worker_id}` and `claim_next_item(db, worker_id)` to return no item unless the run is dispatch-enabled, the caller is the selected online worker, and no foreign/unknown claim is still draining. Keep `worker_id` optional at the HTTP schema boundary only so legacy `{}` requests safely return `item: null` instead of bypassing the gate or failing noisily.
- Include `dispatch_enabled` in `BrowserCollectionRunRead`; do not include claim tokens in run or dashboard serialization.
- Selecting a different worker on an active run atomically disables dispatch until a separate start/resume action; the old worker may finish its current token-owned item. Releasing an expired lease before start validation prevents an abandoned claim from blocking handoff indefinitely.

- [x] **Step 1: Add failing backend tests**

Add tests named `test_scheduled_run_is_created_held`, `test_scheduler_and_collect_all_leave_run_held`, `test_collect_all_and_retry_after_completion_leave_run_held`, `test_held_run_rejects_legacy_and_unselected_claims`, `test_start_requires_selected_online_worker`, `test_only_selected_online_worker_can_claim`, `test_non_selected_heartbeat_cannot_change_owner`, `test_selected_worker_offline_does_not_fail_over`, `test_handoff_disables_dispatch_until_old_claims_drain`, `test_concurrent_selected_worker_claims_are_disjoint`, and `test_browser_control_migration_upgrades_and_downgrades_on_disposable_sqlite`. Assert the scheduled run and collect-all response leave `dispatch_enabled` false; missing/other/legacy worker claims return no item; start returns 409 when the owner is missing/offline or a foreign/unknown claim is running; changing owners blocks new claims until explicit start after drain; a terminal run does not resume when later work is enqueued; the migration preserves existing run/item rows and downgrades cleanly in a disposable DB.

- [x] **Step 2: Run the focused tests and confirm the expected failures**

Run: `pytest tests/test_browser_collection.py tests/test_scheduler.py tests/test_schema.py -q`

Expected: FAIL because worker/control models, API routes, and the run dispatch gate do not exist yet.

- [x] **Step 3: Implement the additive migration and backend gate**

In the migration, seed exactly one control row with no selected worker; add the run gate with a false server default and nullable claim-owner/token columns without modifying or deleting existing queue rows. Implement worker heartbeat/selection, `start-today`, and claim enforcement under the control/run row locks. Keep `ensure_browser_collection_run`, scheduler creation, `queue_all_store_collections`, retry, and page recheck from enabling dispatch. Make explicit resume/start enable dispatch only after the same owner/online/drain checks.

- [x] **Step 4: Run backend and migration regression tests**

Run: `pytest tests/test_browser_collection.py tests/test_scheduler.py tests/test_schema.py tests/test_api.py -q`

Expected: PASS; existing tests that intentionally claim work must explicitly select/register a test worker and start the run. New held-state tests must retain the default false gate.

### Task 2: Propagate Worker Identity and Fence Collection Results

**Files:**
- Modify: `app/api/schemas.py`
- Modify: `app/api/router.py`
- Modify: `app/services/browser_collection.py`
- Modify: `extension/background.js`
- Modify: `extension/dashboard_bridge.js`
- Test: `tests/test_browser_collection.py`
- Test: `tests/extension_background_worker.test.cjs`
- Test: `tests/extension_dashboard_bridge.test.cjs`

**Interfaces:**
- Extend `/api/browser-collection/heartbeat` with `worker_id`, `worker_label`, `extension_version`, and active `{item_id, claim_token}` pairs; use the existing two-minute heartbeat freshness threshold for online state. Send this heartbeat on each enabled worker poll even when the queue is held, so the selector can display online state without claiming work.
- Return a claim token as a top-level field in the claim response, not in dashboard run/item serialization.
- Require the claiming `worker_id` and current `claim_token` on scheduled snapshot, store-discovery, failure, challenge, and lease-renewal writes; reject mismatches/expired attempts with HTTP 409 before mutating data.
- Persist a stable `browserCollectionWorkerId` in `chrome.storage.local`; derive `Galore主机` for the isolated local profile and `第二台电脑` for the enabled LAN worker from the dashboard-context URL mode.

- [x] **Step 1: Add failing API and extension tests**

Add `test_handoff_drains_old_claim_and_rejects_stale_result`, `test_wrong_worker_cannot_finish_claimed_item`, `test_expired_claim_token_cannot_write_snapshot`, `test_worker_identity_is_stable_per_extension_profile`, `test_claim_sends_worker_id_and_result_sends_claim_token`, and `test_held_worker_does_not_open_collection_window_or_item_tab`. Verify the token is absent from `/runs/today` and dashboard serialization.

- [x] **Step 2: Run the focused tests and confirm the expected failures**

Run: `pytest tests/test_browser_collection.py -q` and `node --test tests/extension_background_worker.test.cjs tests/extension_dashboard_bridge.test.cjs`

Expected: FAIL because claims and write-backs do not yet carry worker identity or attempt tokens.

- [x] **Step 3: Implement claim-token validation and extension identity propagation**

Generate a fresh `secrets.token_urlsafe(32)` token per claim and store it with the item attempt. Validate worker/token before all scheduled-item mutations; lease heartbeat renews only matching active item/token pairs. In the extension, generate the worker ID once, attach it to claims/heartbeats, retain each active and prefetched item's token separately from dashboard data, and send both values with all result/failure/challenge reports. In `pollAutomation`, do not create an automation window or item tab until the claim endpoint returns an item.

- [x] **Step 4: Run extension and backend fencing tests**

Run: `pytest tests/test_browser_collection.py -q` and `node --test tests/extension_background_worker.test.cjs tests/extension_dashboard_bridge.test.cjs`

Expected: PASS; old, wrong-worker, and expired-token writes are rejected without saving snapshots or metrics, while the current claim completes normally.

### Task 3: Add Computer Selector and Explicit Start Controls

**Files:**
- Modify: `app/dashboard/router.py`
- Modify: `app/templates/dashboard.html`
- Modify: `app/static/dashboard.js`
- Modify: `app/static/dashboard.css`
- Test: `tests/test_dashboard.py`
- Test: `tests/test_api.py`

**Interfaces:**
- Render the registered worker list and selected worker into the existing `每日浏览器采集` panel; add `#browser-collector-select`, `#browser-run-start`, and a separate preparation/recheck action.
- Worker selection calls `POST /api/browser-collection/control/worker`; selecting alone never emits `aliexpress-monitor:browser-run-requested`.
- `#browser-run-start` calls `POST /api/browser-collection/runs/start-today` or explicit resume, then emits the wake event only after the API confirms dispatch is enabled.
- `立即采集全部` and `重新检查今日任务` remain preparation actions and never dispatch/wake the daily worker.
- Show `待启动`, `采集中`, `已完成`, selected worker online/offline, and “等待旧任务结束” states; disable start when there is no selected online worker or a foreign claim is draining.

- [x] **Step 1: Add failing dashboard and action tests**

Add `test_dashboard_renders_worker_selector_and_separate_start_button`, `test_dashboard_disables_start_for_offline_worker`, `test_worker_selection_does_not_start_queue`, `test_dashboard_refresh_does_not_select_or_start_worker`, and `test_queue_preparation_does_not_dispatch_or_wake_worker`. Assert selection and start are separate controls/API calls, dashboard refresh does not mutate control state, and no preparation/recheck handler emits the worker wake event. Keep the preparation action labeled `重新检查今日任务`; it calls ensure/recheck only.

- [x] **Step 2: Run the focused tests and confirm the expected failures**

Run: `pytest tests/test_dashboard.py tests/test_api.py -q`

Expected: FAIL because the control endpoint data and selector/start controls are not present.

- [x] **Step 3: Implement the dashboard controls and state rendering**

Add the worker selector and explicit start action to the highlighted daily-collection panel. Keep the existing Galore launcher as a launch-only link. Make owner switching show a confirmation when the queue is active; keep the queue held after selection and require the separate start action. Keep preparation and recheck actions from releasing dispatch.

- [x] **Step 4: Run dashboard regression tests**

Run: `pytest tests/test_dashboard.py tests/test_api.py -q`

Expected: PASS; existing dashboard navigation, Galore launch, collect-all, pause, cancel, and verification-resume behavior remains valid under the explicit gate.

### Task 4: Full Verification and Rollout Notes

**Files:**
- Modify: `extension/README.md`
- Modify: `README.md`
- Test: `tests/extension_background_worker.test.cjs`
- Test: `tests/extension_dashboard_bridge.test.cjs`
- Test: `tests/test_browser_collection.py`
- Test: `tests/test_dashboard.py`

- [x] **Step 1: Update operator documentation**

Document that daily tasks are prepared but held, the user chooses an online worker then clicks start, switching workers requires explicit selection/start, and human verification remains manual. State that old extensions without worker identity cannot claim after rollout.

- [x] **Step 2: Run focused suites and the complete regression suites**

Run: `pytest tests/test_browser_collection.py tests/test_scheduler.py tests/test_schema.py tests/test_api.py tests/test_dashboard.py -q` and `node --test tests/extension_*.test.cjs`.

Expected: PASS, including all preexisting focused behavior tests after they explicitly opt into a test worker/run.

- [x] **Step 3: Review migration and side-effect boundaries**

Inspect the final diff and migration downgrade. Confirm it does not drop or rewrite existing queue items, snapshots, metrics, or non-browser queue tables. Do not run Alembic against the live database, rebuild/restart containers, reload either Chrome extension, launch Chrome, or start/clear a live queue; each deployment action requires separate authorization.
