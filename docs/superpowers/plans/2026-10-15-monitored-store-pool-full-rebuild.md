# Monitored Store Pool Full Rebuild Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an explicit, resumable rebuild that replaces active monitored-product membership for each active store only after a complete, identity-validated scan of at most 60 Orders candidates.

**Architecture:** Keep reset batches, per-store checkpoints, and staged candidates separate from `BrowserCollectionRun` and its existing daily items. The selected compatible extension scans one Orders page per claim and posts its result to the backend; the backend persists the checkpoint and performs a per-store transactional membership replacement only after the candidate range is complete. Daily collection and reset dispatch share the existing selected worker but are mutually exclusive while either has an active claim.

**Tech Stack:** FastAPI, SQLAlchemy, Alembic, Pydantic, PostgreSQL/SQLite test schema, Chrome MV3 extension JavaScript, dashboard Jinja/vanilla JavaScript, pytest, Node.js `node:test`.

**Spec:** [2026-10-15-monitored-store-pool-full-rebuild-design.md](../specs/2026-10-15-monitored-store-pool-full-rebuild-design.md)

## Global Constraints

- Snapshot only stores whose status is `active` when the batch is created; leave inactive stores and their products untouched.
- Freeze one reset target date and exactly the seven immediately preceding complete dates for the entire batch.
- Scan no more than the first 60 candidates on Orders-sorted store pages; select no more than 20 eligible products per store.
- Rank by seven-day `daily_increment` sum descending, Orders rank ascending, then product ID ascending.
- A fully scanned store may enroll fewer than 20 eligible products; do not fabricate or infer missing IXSPY values.
- Preserve historical records; deactivate deselected products, reactivate selected existing product records, and never physically delete products or history.
- Keep the existing daily run, its items, diagnostics, and fill-empty-slots behavior unchanged.
- Creating a reset batch must not start dispatch or open a browser tab; worker selection and explicit start are required.
- Require exact store identity and a compatible extension capability before claim or write; no product-detail navigation.
- Do not restart services, reload extensions, change the selected worker, or start/reset real work as part of implementation or test execution.
- Preserve all pre-existing dirty-worktree changes; inspect overlapping diffs before editing, and never stage a whole shared file if that would include unrelated existing hunks.

## Review Focus

- Orders page changes identity after a redirect or CAPTCHA: reject the page and leave that store's existing pool untouched. Pin in Task 3 API identity tests and Task 4 extension navigation tests.
- The same product is listed on multiple scanned pages or has conflicting ownership: stage it once and report the duplicate/conflict without cross-store reassignment. Pin in Task 2 ranking/replacement tests and Task 3 page-staging tests.
- A chart is absent, cumulative-like, or missing one of the frozen seven dates: exclude it with a diagnostic and preserve the existing pool if the scan itself is incomplete. Pin in Task 2 candidate eligibility tests and Task 3 incomplete-page tests.
- A worker disappears, submits a stale token, or retries the same page after a lost response: resume from the last committed page without duplicating candidates or applying a store replacement twice. Pin in Task 3 lease/idempotency tests and Task 4 worker-restart tests.
- A store is deactivated or the daily queue is claimed while a reset is in progress: skip the deactivated store and prevent simultaneous collection without mutating daily-run records. Pin in Task 3 state-transition tests.

---

### Task 1: Add Reset Batch and Store Checkpoint Persistence

**Files:**
- Modify: `app/db/models.py`
- Modify: `app/api/schemas.py`
- Create: `alembic/versions/20261015_0012_store_pool_reset.py`
- Test: `tests/test_store_pool_reset.py`

**Interfaces:**
- Produces `StorePoolResetBatch`: frozen `target_date`, `window_start`, `window_end`, status (`PENDING`, `RUNNING`, `NEEDS_VERIFICATION`, `COMPLETED`, `PARTIAL`, `FAILED`, `CANCELLED`), dispatch flag, and timestamps.
- Produces `StorePoolResetStore`: one row per captured store, with status (`PENDING`, `RUNNING`, `NEEDS_VERIFICATION`, `SUCCEEDED`, `FAILED`, `SKIPPED`, `CANCELLED`), store/platform ID and Orders URL snapshots, position, candidate/page counters, per-page attempt count, next-page URL, visited URLs, staged candidates, diagnostics, claim worker/token/lease, last accepted claim/result digest for idempotency, and terminal error fields.
- Produces Pydantic read/create/page/claim models for later tasks. The create model must require `confirm_replace=True`; page candidate data reuses `BrowserStoreCandidatePage` and `BrowserStoreProduct` validation.
- Adds an extension capability collection to `BrowserCollectionWorker` and its heartbeat/read schemas; the reset capability token is `store_pool_reset_v1`.

- [ ] **Step 1: Write failing persistence and schema tests**

Add tests that create a batch and two store checkpoints through the test session, round-trip frozen window and JSON checkpoint data, reject an unconfirmed create request, and keep worker capability data empty by default for old extensions.

- [ ] **Step 2: Run the focused tests and confirm the missing schema/model failures**

Run: `.\.venv\Scripts\python.exe -m pytest --basetemp .pytest-pool-reset tests/test_store_pool_reset.py -q`
Expected: FAIL because reset models and request/read schemas do not yet exist.

- [ ] **Step 3: Add models, schemas, and migration**

Create the two reset tables with foreign keys and uniqueness for one store checkpoint per batch; add claim/status indexes; add worker capability persistence. The migration must chain from `20261012_0011` and preserve existing rows.

- [ ] **Step 4: Run persistence and migration-focused tests**

Run: `.\.venv\Scripts\python.exe -m pytest --basetemp .pytest-pool-reset tests/test_store_pool_reset.py -q`
Run: `.\.venv\Scripts\python.exe -m alembic heads`
Expected: PASS; test schema creates the reset tables, and `alembic heads` reports the new migration as the sole head.

- [ ] **Step 5: Commit only the persistence slice**

Stage only this task's hunks from the model/schema files plus the migration and new test. Do not stage pre-existing hunks from shared files; if they cannot be separated safely, leave this slice uncommitted and report the overlap. Use commit message `feat: add monitored store pool reset persistence` when staging is isolated.

### Task 2: Rank Candidates and Replace One Store Atomically

**Files:**
- Create: `app/services/store_pool_reset.py`
- Test: `tests/test_store_pool_reset.py`
- Reference: `app/services/monitored_store_collection.py`, `app/services/historical_metrics.py`, `app/services/snapshots.py`

**Interfaces:**
- Define `RankedPoolResetCandidate` in `app/services/store_pool_reset.py` with `product: BrowserStoreProduct`, `orders_rank: int`, and `page_url: str`.
- `rank_pool_reset_candidates(candidates: list[RankedPoolResetCandidate], window_start: date, window_end: date) -> tuple[list[RankedPoolResetCandidate], list[dict]]` returns eligible rows ordered by `(-seven_day_sum, orders_rank, product_id)`, capped at 20, plus per-candidate diagnostics.
- `replace_store_pool_for_store(db, batch: StorePoolResetBatch, store_task: StorePoolResetStore, selected_candidates: list[RankedPoolResetCandidate]) -> dict` rechecks that the store is still active, then applies membership and discovery updates in the caller's transaction; it must not commit independently.
- Candidate eligibility requires exactly the seven frozen dates, all points tagged `daily_increment`, `history_source == "ixspy_visible_daily_increment"`, and no cumulative-like series. Duplicate product IDs and product URLs owned by another store are diagnosed and excluded.

- [ ] **Step 1: Add failing ranking tests**

Cover descending sum, Orders-rank and product-ID tie breaks, missing/duplicate dates, cumulative-like values, wrong history source/value type, duplicate IDs, and the 20-product cap.

- [ ] **Step 2: Run ranking tests and confirm they fail**

Run: `.\.venv\Scripts\python.exe -m pytest --basetemp .pytest-pool-reset tests/test_store_pool_reset.py -k ranking -q`
Expected: FAIL because `rank_pool_reset_candidates` is not implemented.

- [ ] **Step 3: Implement pure ranking and diagnostics**

Reuse existing date/value validation helpers where their semantics match the frozen reset window; return deterministic ranking and per-candidate exclusion reasons.

- [ ] **Step 4: Add failing transactional replacement tests**

Assert that successful replacement deactivates nonselected active rows, reactivates matching inactive rows, inserts new rows, updates discovery metadata and imports the selected candidates' visible history, while retaining existing snapshots, daily metrics, reviews, and collection attempts. Assert an inactive store causes no membership changes.

- [ ] **Step 5: Implement `replace_store_pool_for_store`**

Use the existing snapshot and historical-sales import helpers with `commit=False`; never delete product or history rows. Leave commit/rollback to the page-result transaction in Task 3.

- [ ] **Step 6: Run ranking and replacement tests**

Run: `.\.venv\Scripts\python.exe -m pytest --basetemp .pytest-pool-reset tests/test_store_pool_reset.py -k "ranking or replacement" -q`
Expected: PASS, including history preservation and per-store atomicity assertions.

- [ ] **Step 7: Commit only the ranking/replacement slice**

Stage only this task's service and test hunks; do not stage pre-existing changes in the shared test file. If the hunks cannot be separated safely, leave this slice uncommitted and report the overlap. Use commit message `feat: rank and atomically replace monitored store pools` when staging is isolated.

### Task 3: Implement Batch Lifecycle, Page Checkpoints, and Queue Exclusivity

**Files:**
- Modify: `app/services/store_pool_reset.py`
- Modify: `app/services/browser_collection.py`
- Modify: `app/api/router.py`
- Modify: `app/api/schemas.py`
- Test: `tests/test_store_pool_reset.py`
- Test: `tests/test_browser_collection.py`

**Interfaces:**
- `create_pool_reset_batch(db, *, confirm_replace: bool) -> StorePoolResetBatch` snapshots active stores and the common seven-date window; creation is held.
- `start_pool_reset_batch(db, batch_id) -> StorePoolResetBatch` requires the selected worker to be online and advertise `store_pool_reset_v1`, no active daily claim, and no other active reset.
- `claim_pool_reset_store(db, worker_id) -> (batch, store_task, claim_token, candidate_limit) | None` leases one store-page continuation to the selected worker; allow only one `RUNNING` store task in a batch at a time.
- `record_pool_reset_page(db, task_id, worker_id, claim_token, page) -> StorePoolResetBatch` validates claim/store/Orders URL/page identity and completeness, stages once, then requeues the same store at its next page or atomically replaces its pool when the first 60 candidates or all available pages are exhausted.
- Add `POST /browser-collection/pool-resets` (body requires `confirm_replace`), `GET /browser-collection/pool-resets/latest`, `POST /browser-collection/pool-resets/{batch_id}/start`, `POST /browser-collection/pool-resets/{batch_id}/cancel`, `POST /browser-collection/pool-resets/claim`, `POST /browser-collection/pool-resets/heartbeat`, and `POST /browser-collection/pool-resets/tasks/{task_id}/page`, `/challenge`, `/resume`, and `/failure`.
- The claim response includes `candidate_limit = min(50, 60 - scanned_candidate_count)`; assign each staged candidate its global one-based Orders rank from prior committed count plus its page position. Preserve first-seen rank for duplicates and diagnose later duplicates.
- Advance only when the page's card enumeration and chart-read attempt are complete, pagination is known, and any advertised next URL is valid, same-store, Orders-sorted, and not already visited. A valid terminal page can finalize with fewer than 60 candidates; unknown/incomplete pagination cannot replace the pool.
- The reset heartbeat refreshes the selected worker and active reset lease without mutating the daily run's heartbeat/status/items/errors. During reset collection the extension uses this reset heartbeat rather than the daily-run heartbeat path.
- A challenge keeps the same store task and claim bound to the same page, stops claims for other stores, and exposes `NEEDS_VERIFICATION`; after human verification, the extension calls `/resume` only after it revalidates the target store and page URL.
- Worker-loss retries follow the existing store-task bound of two attempts per uncommitted page; reset the page attempt counter only after accepting a complete page. A user retry of a failed store resumes at the first uncommitted page; it never reuses another store's staged candidates.
- A repeated accepted result with the same claim token and page-result digest returns the already-recorded outcome; a stale token with different content is rejected and cannot stage or replace anything.
- Daily `start_browser_collection_run` and `claim_next_item` must reject/withhold claims while reset dispatch owns the browser; reset start must reject an actively claimed daily task. Do not modify daily-run rows as a side effect.

- [ ] **Step 1: Add failing batch lifecycle and API tests**

Test exact active-store snapshotting, frozen dates across simulated midnight, held creation, explicit start, online/selected/capable worker requirements, inactive-store exclusion, and rejection when a daily store task is actively claimed.

- [ ] **Step 2: Run lifecycle tests and confirm expected failures**

Run: `.\.venv\Scripts\python.exe -m pytest --basetemp .pytest-pool-reset tests/test_store_pool_reset.py -k "batch or worker or lifecycle" -q`
Expected: FAIL because the lifecycle service and API routes are absent.

- [ ] **Step 3: Implement held batch creation, start, cancellation, and status serialization**

Lock the existing browser-collection control row while creating/starting batches so double clicks cannot create overlapping active resets. Snapshot only active stores and leave daily-run status/items/errors untouched.

- [ ] **Step 4: Add failing page checkpoint and atomicity tests**

Test valid page staging and continuation, first-60 boundary across pages, incomplete page cursor not advancing, URL/store-ID mismatch, stale worker/token rejection, duplicate page-result idempotency, retry from last committed page, and store-level replacement rollback when persistence fails.

- [ ] **Step 5: Implement claim leases, page staging, retries, and final per-store transaction**

Persist page candidates and diagnostics on the store checkpoint. Validate that each submitted URL is the exact expected Orders page for the task's store; a page must be complete before advancing. On finalization, call Task 2 ranking/replacement and mark the store terminal in the same transaction. Keep replacement isolated per store.

- [ ] **Step 6: Enforce mutual exclusion in daily start/claim and expose reset API routes**

Use the same control-row locking discipline in both queue paths. Keep the daily run response and item schema unchanged; reset claims are available only from the dedicated reset endpoint.

- [ ] **Step 7: Run backend queue and reset suites**

Run: `.\.venv\Scripts\python.exe -m pytest --basetemp .pytest-pool-reset tests/test_store_pool_reset.py tests/test_browser_collection.py -q`
Expected: PASS; old daily tests remain unchanged and new tests prove no simultaneous claims or daily-row mutation.

- [ ] **Step 8: Commit only the reset lifecycle/API slice**

Stage only this task's hunks in shared services, API/schema, and test files. If a pre-existing hunk cannot be excluded safely, leave this slice uncommitted and report it. Use commit message `feat: add exclusive monitored store reset workflow` when staging is isolated.

### Task 4: Add One-Orders-Page-at-a-Time Extension Collection

**Files:**
- Modify: `extension/manifest.json`
- Modify: `extension/background.js`
- Modify: `extension/page_collector.js`
- Test: `tests/extension_background_worker.test.cjs`
- Test: `tests/extension_page_collector.test.cjs`
- Test: `tests/extension_store_collector.test.cjs`

**Interfaces:**
- Heartbeats advertise `store_pool_reset_v1`; older workers without this capability remain ineligible and cannot claim reset work.
- Add reset claim/page/heartbeat/challenge/failure API calls using the Task 3 routes. Store reset automation state with task kind `POOL_RESET`, distinct from daily `STORE` and `PRODUCT` tasks.
- Each claim scans exactly its assigned Orders page, returns a complete candidate-page payload, and submits it with worker ID, claim token, expected store ID, and page URL. The backend, not extension-local storage, owns reset candidate staging and pagination checkpoint.

- [ ] **Step 1: Add failing extension protocol tests**

Assert that a selected compatible worker claims reset pages only when no other task is active; repeated polling cannot open duplicate tabs; reset submission contains the exact task/store/page/token; untrusted or incomplete page results are not submitted; no product-detail URL is opened.

- [ ] **Step 2: Run focused Node tests and confirm expected failures**

Run: `node --test tests/extension_background_worker.test.cjs tests/extension_page_collector.test.cjs tests/extension_store_collector.test.cjs`
Expected: FAIL on missing capability and reset-task handling.

- [ ] **Step 3: Add the capability and reset-task page collector path**

Reuse the existing visible IXSPY Orders-page candidate scanner and chart parser; preserve foreground/maximized collection behavior and the one-page-per-claim bound. Do not use product-detail navigation or the current daily pool-fill candidate cursor.

- [ ] **Step 4: Integrate reset claims with automation, heartbeat, verification, and failure recovery**

Add explicit `POOL_RESET` branches to poll, open, process, heartbeat, challenge resume, and stale-claim cleanup. A manual platform challenge must retain the current store/page task and resume only after the page identity is revalidated.

- [ ] **Step 5: Run extension tests and syntax checks**

Run: `node --test tests/extension_background_worker.test.cjs tests/extension_page_collector.test.cjs tests/extension_store_collector.test.cjs`
Run: `node --check extension/background.js`
Run: `node --check extension/page_collector.js`
Expected: all tests pass and both JavaScript files parse successfully.

- [ ] **Step 6: Commit only the extension slice**

Stage only this task's hunks in extension sources and tests; do not include prior extension fixes in the same dirty files. If the hunks cannot be separated safely, leave this slice uncommitted and report the overlap. Use commit message `feat: collect monitored pool reset candidates by store page` when staging is isolated.

### Task 5: Add Explicit Dashboard Controls and Progress

**Files:**
- Modify: `app/dashboard/router.py`
- Modify: `app/templates/dashboard.html`
- Modify: `app/static/dashboard.js`
- Modify: `app/static/dashboard.css`
- Modify: `app/api/schemas.py` if read-model fields need refinement
- Test: `tests/test_dashboard.py`
- Test: `tests/test_store_pool_reset.py`

**Interfaces:**
- Render reset state from `GET /api/browser-collection/pool-resets/latest`; show captured store count, frozen date window, per-store status, candidate/page progress, and failure diagnostics separately from the daily queue.
- Add an explicit “重建监控商品池” action with a confirmation that membership will be replaced while history is retained. Confirmation creates a held batch only.
- Reuse the selected computer control, but require a separate “开始重建” click; add cancel for unfinished reset work. Refresh status without changing daily queue state.

- [ ] **Step 1: Add failing dashboard render and interaction tests**

Test the idle state, pending-but-not-started batch, incompatible/offline worker state, running progress, partial/failure diagnostics, confirmation cancellation, explicit start, and reset cancellation. Assert reset controls never call daily collect-all/start endpoints.

- [ ] **Step 2: Run dashboard tests and confirm they fail**

Run: `.\.venv\Scripts\python.exe -m pytest --basetemp .pytest-pool-reset tests/test_dashboard.py -k pool_reset -q`
Expected: FAIL because reset state and controls are not rendered.

- [ ] **Step 3: Add dashboard context and a separate reset progress section**

Keep the daily collection section and current status filters intact. Add the reset block as a separate, compact section using existing dashboard styling and status conventions.

- [ ] **Step 4: Implement confirmation, held creation, selected-worker start, polling, and cancel interactions**

Use only reset endpoints for reset actions. After creation, show pending status and do not dispatch the browser-run event or open pages until the dedicated start action succeeds.

- [ ] **Step 5: Run dashboard tests and JavaScript syntax check**

Run: `.\.venv\Scripts\python.exe -m pytest --basetemp .pytest-pool-reset tests/test_dashboard.py -k pool_reset -q`
Run: `node --check app/static/dashboard.js`
Expected: PASS; daily queue control tests remain unchanged.

- [ ] **Step 6: Commit only the dashboard slice**

Stage only this task's hunks in dashboard files and tests; if pre-existing changes overlap, do not stage them. If isolation is unsafe, leave this slice uncommitted and report it. Use commit message `feat: add monitored pool reset controls and progress` when staging is isolated.

### Task 6: Verify Cross-Queue and Data-Preservation Acceptance

**Files:**
- Test: `tests/test_store_pool_reset.py`
- Test: `tests/test_browser_collection.py`
- Test: `tests/extension_background_worker.test.cjs`
- Test: `tests/test_dashboard.py`

- [ ] **Step 1: Add an end-to-end API test for a two-store batch**

Create a held batch with one complete 60-candidate store and one failing store. Assert the first store commits at most 20 ranked candidates, the second retains its old pool, inactive stores remain unchanged, and the daily run's status, items, and errors are unchanged by reset operations.

- [ ] **Step 2: Add an end-to-end worker compatibility and explicit-start test**

Assert an old worker cannot start/claim reset work; a compatible selected worker can claim only after start; queue creation alone causes no claim; and daily tasks cannot be claimed while reset dispatch owns the browser.

- [ ] **Step 3: Run the full relevant verification set**

Run: `.\.venv\Scripts\python.exe -m pytest --basetemp .pytest-pool-reset tests/test_store_pool_reset.py tests/test_browser_collection.py tests/test_dashboard.py -q`
Run: `node --test tests/extension_background_worker.test.cjs tests/extension_page_collector.test.cjs tests/extension_store_collector.test.cjs`
Run: `node --check extension/background.js`
Run: `node --check extension/page_collector.js`
Run: `node --check app/static/dashboard.js`
Expected: all focused suites pass. Report any unrelated existing failures without changing unrelated files.

- [ ] **Step 4: Review migration and staged diff for scope**

Verify the migration chains from `20261012_0011`, no code deletes product/history rows, no daily-run item is rewritten, and only files listed in this plan are staged for these commits.

- [ ] **Step 5: Commit only the acceptance tests**

Stage only this task's new acceptance-test hunks; leave pre-existing test edits unstaged. If they cannot be separated safely, leave the tests uncommitted and report the overlap. Use commit message `test: verify monitored pool reset isolation and preservation` when staging is isolated.

## Implementation Boundary

This plan includes creating one real reset batch in the held `PENDING` state after the new backend/API is available. Batch creation does not start dispatch or open Chrome tabs. It does not modify monitored products, resume or cancel the current daily queue, restart backend services, reload either computer's extension, or create an extension archive. Live deployment/restart remains a separate approval step; if unavailable, report the batch creation as pending deployment rather than writing directly to production tables.
