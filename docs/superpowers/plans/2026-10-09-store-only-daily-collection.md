# Store-only Daily Collection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collect each monitored store's daily IXSPY increments from its Orders-sorted listing pages without scheduled product-detail visits or monitoring-pool changes.

**Architecture:** The existing daily run keeps one `STORE` item per monitored store. The claim supplies the active product IDs; the extension visits same-store listing pages, samples only matching cards, and submits one guarded store result. Missing data stays provisional and is retried on a later store-page run.

**Tech Stack:** FastAPI, SQLAlchemy, PostgreSQL/SQLite tests, Chrome MV3 extension, Node test runner.

**Spec:** `docs/superpowers/specs/2026-10-08-store-only-daily-collection-design.md`

## Global Constraints

- Never open an item detail URL in the scheduled store path. Keep explicit `product:{id}:single` diagnosis separate.
- Do not create/deactivate/reorder monitored products from a scheduled result.
- Preserve the paused/cancelled live queue and dirty worktree; no deployment, extension reload, cleanup, or commit during source implementation.
- A visible IXSPY zero is confirmed; an unobserved date may be stored as `estimated_sales=0`, `is_estimable=false`, `reason=ixspy_store_page_pending` and is not counted in store totals.
- No CAPTCHA bypass. A challenge pauses the same task for human action.

## Review Focus

- Target IDs change during a run: reject the stale submission without changing metrics or the pool (Task 3).
- Next-page link loops, leaves the store, or loses Orders sort: stop with a partial result, never follow it (Task 4).
- Chart reports cumulative-like values or another product's ID: reject that product's series (Tasks 3 and 4).
- Old extension submits its first 20 Orders products: fail closed instead of replacing the seven-day-ranked pool (Task 3).
- A real zero and an unobserved date both contain numeric zero: preserve different `is_estimable` and reason values (Task 3).

---

### Task 1: Store-only queue lifecycle

**Files:** Modify `app/services/browser_collection.py`; test `tests/test_browser_collection.py`.

**Interfaces:** Keep existing `ensure_browser_collection_run`, `queue_store_collection`, `claim_next_item`, and explicit `queue_single_product_collection` signatures. Introduce an internal skip of legacy automatic `product:{id}` items, leaving `product:{id}:single` untouched.

- [ ] Add tests: scheduled run creates only store items for active stores with active monitored products; completing stores creates no product items; advancing an older run skips pending automatic product items but not manual tests; a cancelled run's store button requeues only the selected store and leaves other skipped items skipped.
- [ ] Run the new tests and confirm they fail for the old fallback/cancel behavior.
- [ ] Remove automatic product-item creation and retry, update phase advancement and cancelled-run single-store reopening; preserve already paused/cancelled runs until an explicit action advances them.
- [ ] Run `tests/test_browser_collection.py`, updating old tests that asserted the retired fallback while retaining tests for explicit single-product diagnosis.

### Task 2: Fixed monitored-ID claim contract

**Files:** Modify `app/api/router.py`, `app/api/schemas.py`, `app/services/browser_collection.py`; test `tests/test_browser_collection.py`.

**Interfaces:** `BrowserCollectionItemRead.monitored_product_ids: list[str] = []` and `serialize_item(item, *, include_targets=False)`. Only the claim response includes the sorted active IDs for `STORE`; other queue views remain compact.

- [ ] Add a failing claim test for exactly the store's active IDs, excluding inactive/other-store products, and an empty-store queue test.
- [ ] Implement the target-list serialization and exclude empty stores from scheduled runs. The single-store button returns a clear intake-required error when no monitored products exist.
- [ ] Run focused backend tests, then the browser-collection suite.

### Task 3: Guarded store-only result and provisional metrics

**Files:** Create `app/services/monitored_store_collection.py`; modify `app/api/router.py`, `app/api/schemas.py`, `app/services/historical_metrics.py` only if a shared provisional upsert is needed; test `tests/test_browser_collection.py`, `tests/test_collection_channels.py`, `tests/test_historical_metrics.py`.

**Interfaces:** The queued `store-discovery` endpoint accepts `raw_data.collection_mode="monitored_store_daily"`, `raw_data.monitored_product_ids`, `raw_data.visited_page_urls`, and up to 20 matched `products`. A missing target can be absent from `products`. It calls `save_monitored_store_observation(db, item, payload) -> (snapshot, complete: bool, diagnostics: dict)`; the manual ranked-selection endpoint retains its own behavior.

- [ ] Add failing tests for pool immutability, unexpected/stale IDs, old extension payload, wrong store, partial seven-day values, confirmed zero, pending zero, and replacement of pending zero by a later confirmed IXSPY value.
- [ ] Implement validation before any write. Persist observed points and provisional missing dates without overwriting confirmed metrics. Record target/coverage diagnostics; finish `SUCCEEDED` only for all target IDs with 7/7 direct values, otherwise `PARTIAL`.
- [ ] Recompute affected store totals from confirmed values only. Run focused backend tests and the full backend suite.

### Task 4: Same-store pagination in the extension

**Files:** Modify `extension/page_collector.js`, `extension/content.js`, `extension/background.js`, `extension/manifest.json`; test `tests/extension_store_collector.test.cjs`, `tests/extension_background_worker.test.cjs`, `tests/extension_content.test.cjs`.

**Interfaces:** `collect("STORE", targetDate, { monitored_product_ids })` returns one-page `page_result` with matched products and a validated same-store Orders `next_page_url`. The background worker merges pages into the Task 3 submission contract. Limits: 10 store pages and 8 minutes collection work; the worker's store timeout is 10 minutes and backend store runtime limit 12 minutes.

- [ ] Add failing collector tests that target only monitored IDs, preserve product-ID binding, and return a safe next-page URL without selecting carousel controls.
- [ ] Add failing worker tests for two-page merge, no detail-page navigation, repeated/foreign/unresolvable next link, challenge stop, and missing target diagnostics.
- [ ] Implement per-page collection and bounded worker navigation. Never follow an item URL or treat missing as observed zero. Bump the extension version.
- [ ] Run all extension tests and `node --check` for changed JS files.

### Task 5: Dashboard wording and end-to-end contract tests

**Files:** Modify `app/templates/dashboard.html`, `app/static/dashboard.js`, `frontend/src/app.js` where the active queue wording appears; test `tests/test_dashboard.py` and relevant extension/backend suites.

- [ ] Add failing UI/contract tests: one store action queues one store-page item; queue wording does not promise product-page fallback; partial coverage identifies pending values.
- [ ] Update the visible copy and coverage rendering without changing the separate manual top-20 selection control.
- [ ] Run focused UI tests, the full backend and extension suites, `git diff --check`, and compare queue/database state to the initial read-only baseline. Report source-tested versus live-deployed status separately.

## Execution Notes

The workspace is already heavily dirty. Make narrow `apply_patch` edits, do not reset or clean files, and do not make a commit that absorbs prior unrelated changes. Browser policy blocks agent interaction with the live AliExpress tab; live acceptance must be user-run after separately authorized deployment.
