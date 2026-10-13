# Store-Page Pool Fill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enroll eligible IXSPY products immediately page by page, then use each store's persisted Orders-page cursor to fill open monitoring-pool slots during scheduled store collection without opening product detail pages.

**Architecture:** The backend owns a per-store discovery cursor and validates all page/product evidence. Manual enrollment appends eligible current-page candidates immediately; scheduled store tasks update existing monitored products and scan at most one cursor page for new candidates, preserving all existing pool members. StoreSnapshot records each discovery attempt, while cursor and candidate inserts advance atomically.

**Tech Stack:** Python 3.12, FastAPI, Pydantic, SQLAlchemy, Alembic, PostgreSQL, Chrome Manifest V3 extension, JavaScript, pytest, Node.js built-in test runner.

**Spec:** [2026-10-12-store-page-pool-fill-design.md](../specs/2026-10-12-store-page-pool-fill-design.md)

## Global Constraints

- Pool target is 20 active products per store; automatic enrollment adds only into open slots and does not remove existing products.
- Candidate ranking is the sum of seven complete, directly visible IXSPY `daily_increment` values, descending; Orders-page rank breaks ties.
- A visible zero is valid. Missing dates are not inferred as zero for candidate enrollment.
- Daily collection updates existing monitored products and scans at most one candidate-discovery page per store task.
- Candidate collection stays on the store Orders page and never opens product detail pages.
- Verification, wrong-store identity, invalid Orders sorting, malformed pagination, and page timeout must not advance the cursor.
- A structurally valid page with no eligible candidates advances; unavailable individual candidate charts are skipped with diagnostics and retried after wraparound.
- Do not restart services, mutate the live queue/database, or reload Chrome during source implementation or test execution.
- Preserve pre-existing worktree changes. In particular, read and incorporate the current diffs in `app/services/monitored_store_collection.py`, `extension/background.js`, `extension/page_collector.js`, `extension/manifest.json`, and their modified tests; never stage or revert unrelated files.

## Review Focus

- Empty active pool: the store is still queued and can discover candidates; test `test_scheduled_run_includes_active_store_without_products` in Task 3.
- Wrong-store, unsorted, stale, or malformed page URL: cursor and pool remain unchanged; test `test_invalid_or_stale_store_page_does_not_advance_cursor` in Tasks 1–2.
- Candidate chart has missing dates while other page cards are readable: candidate is skipped without zero-fill, but a complete page scan advances; test `test_incomplete_candidate_is_skipped_and_valid_page_advances` in Task 4.
- Page-level timeout after existing-product data was collected: existing metrics are retained, no partial-page candidate is enrolled, and the candidate cursor is unchanged; test `test_candidate_scan_timeout_preserves_metrics_and_cursor` in Task 4. Challenge handling and cursor retention are tested in Task 6.
- Duplicate submission or legacy pool above 20: no duplicate/reactivation/eviction occurs and no additional candidate is added above the cap; test `test_repeated_submission_and_overfilled_pool_are_append_only` in Task 2.

## File Map

| File | Responsibility |
|---|---|
| `app/db/models.py` | SQLAlchemy model for durable per-store discovery state. |
| `alembic/versions/20261012_0011_store_page_discovery.py` | Create/drop the per-store cursor table. |
| `app/services/store_page_discovery.py` | Cursor initialization/advance, page URL validation, candidate ranking and append-only persistence. |
| `app/api/schemas.py` | Candidate page payload and empty-pool daily request validation. |
| `app/api/router.py` | Manual page enrollment route and queue store-discovery route integration. |
| `app/services/browser_collection.py` | Include empty/underfilled active stores and serialize the candidate cursor into claims. |
| `app/services/monitored_store_collection.py` | Save monitored metrics independently and invoke candidate-page persistence. |
| `extension/store_selection.js` | Rank and build a submission from the current page without waiting for 20 candidates. |
| `extension/popup/popup.js` | Submit each manually collected page immediately and report append/cursor outcome. |
| `extension/background.js` | Update current monitored targets and read one cursor page per store task. |
| `extension/content.js` | Route the scheduled candidate-scan message to the page collector. |
| `extension/page_collector.js` | Sequentially read visible candidate charts and report page readiness/pagination diagnostics. |
| `extension/manifest.json` | Bump extension version for the new page protocol. |
| `tests/test_store_page_discovery.py` | Cursor validation, progression, and page-level candidate persistence tests. |
| `tests/test_collection_channels.py` | Manual extension submission and append-only enrollment API tests. |
| `tests/test_browser_collection.py` | Queue inclusion, empty-pool claim, and scheduled candidate scan persistence tests. |
| `tests/extension_store_selection.test.cjs` | Current-page ranking and empty/partial payload behavior. |
| `tests/extension_popup_store_selection.test.cjs` | Popup submits current page immediately and handles repeat/error states. |
| `tests/extension_background_worker.test.cjs` | Cursor-page routing, bounded navigation, and no-detail-page behavior. |
| `tests/extension_page_collector.test.cjs` | Sequential IXSPY candidate reads and failure diagnostics. |
| `tests/extension_store_collector.test.cjs` | Store page identity, Orders sorting, chart, and pagination extraction. |

---

### Task 1: Durable Store Cursor

**Files:**
- Modify: `app/db/models.py`
- Create: `alembic/versions/20261012_0011_store_page_discovery.py`
- Create: `app/services/store_page_discovery.py`
- Create: `tests/test_store_page_discovery.py`

**Interfaces:**
- Produces `StorePageDiscoveryState` with `store_id`, `first_orders_url`, `next_page_url`, `last_successful_page_url`, `last_scanned_at`, `last_outcome`, and `last_error`.
- Produces `get_or_create_store_page_discovery_state(db: Session, store: Store, *, first_orders_url: str | None = None) -> StorePageDiscoveryState`.
- Produces `advance_store_page_cursor(db: Session, store: Store, *, scanned_url: str, has_next_page: bool, next_page_url: str | None, scan_complete: bool, error: str | None = None) -> StorePageDiscoveryState`. Only a complete, identity-matched Orders page advances; a confirmed last page wraps to `first_orders_url`.

- [ ] **Step 1: Write failing cursor tests**

Add `test_cursor_initializes_from_canonical_orders_url`, `test_complete_page_advances_to_next_url`, `test_confirmed_last_page_wraps_to_first_url`, `test_invalid_or_stale_store_page_does_not_advance_cursor`, and `test_incomplete_page_retains_cursor` in `tests/test_store_page_discovery.py`. Assert exact stored URLs and unchanged state after rejected pages.

- [ ] **Step 2: Run cursor tests and confirm the expected failures**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_store_page_discovery.py -q --basetemp .superpowers/sdd/2026-10-12-store-page-pool-fill/pytest-tmp`

Expected: FAIL because the state model and cursor service are not defined.

- [ ] **Step 3: Implement the state model, migration, and cursor service**

Set migration revision `20261012_0011` with `down_revision = "20261010_0010"`. Use `store_id` as both primary key and cascading foreign key. Normalize and validate every URL as HTTPS AliExpress `/store/{id}/pages/all-items.html` with `shop_sortType=orders_desc`; preserve page query parameters. Reject wrong-store, stale, or incomplete scans without changing `next_page_url`. Use a row lock for state changes where supported.

- [ ] **Step 4: Run focused tests and validate the migration on a disposable database**

Run the focused pytest command from Step 2. Run `alembic upgrade head` and `alembic downgrade -1` only against a disposable migration-test database, then upgrade it again and confirm the cursor table and unique store key exist. Do not point this check at the running monitor database.

- [ ] **Step 5: Commit Task 1 files only**

```bash
git add app/db/models.py alembic/versions/20261012_0011_store_page_discovery.py app/services/store_page_discovery.py tests/test_store_page_discovery.py
git commit -m "feat: persist store discovery cursors"
```

### Task 2: Immediate Append-Only Manual Enrollment

**Files:**
- Modify: `app/api/schemas.py`
- Modify: `app/api/router.py`
- Modify: `app/services/store_page_discovery.py`
- Test: `tests/test_collection_channels.py`
- Test: `tests/test_store_page_discovery.py`

**Interfaces:**
- Produces `save_store_page_candidates(db: Session, store: Store, *, page_url: str, candidates: Sequence[BrowserStoreProduct], has_next_page: bool, next_page_url: str | None, scan_complete: bool, source: str, raw_data: dict) -> dict`.
- Returns `added_count`, `existing_count`, `skipped_count`, `deactivated_count` (always `0` for this workflow), and `continuation_saved`.
- Extend `BrowserStoreDiscoveryRead` with `continuation_saved: bool` so the popup can confirm that the next page was persisted.
- The existing `POST /api/collection/browser-extension/store` route passes the current page to this service; a ranked-selection payload may contain zero eligible products only when it includes a valid scanned Orders page.

- [ ] **Step 1: Write failing route/service tests**

Add `test_ranked_selection_enrolls_fewer_than_twenty_complete_candidates`, `test_zero_eligible_page_is_recorded_and_cursor_saved`, `test_repeated_submission_and_overfilled_pool_are_append_only`, `test_incomplete_or_cumulative_candidates_do_not_mutate_pool`, and `test_invalid_or_stale_page_does_not_mutate_pool_or_cursor` in the existing backend tests. Assert fewer than 20 eligible candidates are added, an empty selection still records page evidence, existing IDs remain active, inactive IDs stay inactive, and no additions occur above 20 active products.

- [ ] **Step 2: Run the focused tests and confirm they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_collection_channels.py tests/test_store_page_discovery.py -k "ranked_selection or zero_eligible_page or repeated_submission or incomplete_or_cumulative or invalid_or_stale_page" -q --basetemp .superpowers/sdd/2026-10-12-store-page-pool-fill/pytest-tmp`

Expected: FAIL because the current route requires exactly `min(20, candidate_count)` products and deactivates absent ranked products.

- [ ] **Step 3: Implement shared append-only candidate persistence**

Validate direct seven-day increments, exact dates, product ID/URL correspondence, store ownership, and cumulative-like series. Rank eligible candidates by page-local seven-day sum descending, then `orders_rank`; append only `max(0, 20 - active_count)`. Persist page evidence even with zero eligible candidates. Never deactivate or reactivate existing product rows in this path. Save candidate metrics and cursor advancement in the same transaction.

- [ ] **Step 4: Run the focused backend tests**

Run the command from Step 2. Expected: PASS, with added count bounded by vacant slots and `deactivated_count == 0`.

- [ ] **Step 5: Commit Task 2 files only**

```bash
git add app/api/schemas.py app/api/router.py app/services/store_page_discovery.py tests/test_collection_channels.py tests/test_store_page_discovery.py
git commit -m "feat: append eligible store page products"
```

### Task 3: Queue Empty and Underfilled Stores

**Files:**
- Modify: `app/services/browser_collection.py`
- Modify: `app/api/schemas.py`
- Modify: `app/api/router.py`
- Test: `tests/test_browser_collection.py`

**Interfaces:**
- Store claims continue to use `BrowserCollectionItemRead`; for `target_type == "STORE"`, include `discovery_page_url: str | None` alongside `monitored_product_ids`.
- An empty `monitored_product_ids` list is valid for a store task when `discovery_page_url` exists; PRODUCT tasks remain unchanged.

- [ ] **Step 1: Write failing queue tests**

Add `test_scheduled_run_includes_active_store_without_products`, `test_manual_store_collection_queues_empty_pool_for_discovery`, and `test_store_claim_exposes_persisted_discovery_cursor`. Assert all active stores appear once, inactive stores do not appear, empty targets are serialized as `[]`, and the cursor URL is the backend's next URL.

- [ ] **Step 2: Run the tests and confirm the expected failures**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_browser_collection.py -k "includes_active_store_without_products or empty_pool_for_discovery or exposes_persisted_discovery_cursor" -q --basetemp .superpowers/sdd/2026-10-12-store-page-pool-fill/pytest-tmp`

Expected: FAIL because queue creation joins to active products and single-store queueing rejects empty pools.

- [ ] **Step 3: Include every active store and serialize its cursor**

Remove the product join from scheduled/manual store enumeration while retaining `Store.status == "active"`. Initialize missing discovery state from `_store_collection_url(store)`. Allow an empty pool for STORE tasks and return its current cursor in the claim payload.

- [ ] **Step 4: Run focused queue tests and existing queue regressions**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_browser_collection.py -q --basetemp .superpowers/sdd/2026-10-12-store-page-pool-fill/pytest-tmp`

Expected: PASS; queue exclusivity, worker ownership, run pausing, and product tasks remain unchanged.

- [ ] **Step 5: Commit Task 3 files only**

```bash
git add app/services/browser_collection.py app/api/schemas.py app/api/router.py tests/test_browser_collection.py
git commit -m "feat: queue stores with open pool slots"
```

### Task 4: Persist Daily Candidate Discovery

**Files:**
- Modify: `app/api/schemas.py`
- Modify: `app/api/router.py`
- Modify: `app/services/monitored_store_collection.py`
- Modify: `app/services/store_page_discovery.py`
- Test: `tests/test_browser_collection.py`

**Interfaces:**
- Add `BrowserStoreCandidatePage` with `page_url: str`, `has_next_page: bool`, `next_page_url: str | None`, `scan_complete: bool`, `candidates: list[BrowserStoreProduct]` (maximum 50), and `diagnostics: dict[str, Any]`.
- Add `candidate_page: BrowserStoreCandidatePage | None` to `BrowserStoreDiscoveryRequest`. Existing monitored-product `products` and candidate-page `candidates` remain separate.
- Validate at most 11 unique visited Orders URLs per store task (the existing 10-page target walk plus one candidate page); duplicate references to the same URL count once.
- `save_monitored_store_observation(...)` continues returning `(StoreSnapshot, complete, diagnostics)`; diagnostics add candidate scan state, added/skipped IDs, and cursor before/after.

- [ ] **Step 1: Write failing daily candidate persistence tests**

Add `test_underfilled_daily_store_adds_candidates_and_advances_cursor`, `test_incomplete_candidate_is_skipped_and_valid_page_advances`, `test_candidate_scan_timeout_preserves_metrics_and_cursor`, `test_empty_pool_daily_store_accepts_candidate_page`, `test_full_pool_skips_candidate_page_without_advancing_cursor`, and `test_candidate_page_url_limit_counts_unique_urls`. Assert candidate additions are bounded by vacancies; existing metric writes survive a `scan_complete=False` candidate result; an incomplete page adds no candidates and does not advance; no more than 11 unique visited URLs are accepted; and zero eligible candidates on a complete page still advance.

- [ ] **Step 2: Run the focused tests and confirm they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_browser_collection.py -k "underfilled_daily_store or incomplete_candidate or candidate_scan_timeout or empty_pool_daily_store or full_pool_skips_candidate_page or candidate_page_url_limit" -q --basetemp .superpowers/sdd/2026-10-12-store-page-pool-fill/pytest-tmp`

Expected: FAIL because daily requests currently have no candidate-page field and the service requires a nonempty monitored target set.

- [ ] **Step 3: Implement bounded candidate-page validation and persistence**

Validate candidate page store identity, Orders sort, cursor match, page structure, candidate IDs/URLs, direct seven-day data, and the 20-product vacancy limit. Save existing monitored-product daily metrics independently of candidate enrollment. For a structurally complete page, append eligible candidates and advance the cursor atomically; for an incomplete page, append no candidates, save diagnostics and existing metrics, and keep the cursor unchanged. A full pool skips candidate discovery while preserving the cursor.

- [ ] **Step 4: Run focused tests and the complete backend suite**

Run the focused command from Step 2, then `.\.venv\Scripts\python.exe -m pytest --basetemp .superpowers/sdd/2026-10-12-store-page-pool-fill/pytest-tmp`

Expected: PASS; existing missing-date semantics, worker diagnostics, and claim-token checks remain intact.

- [ ] **Step 5: Commit Task 4 files only**

```bash
git add app/api/schemas.py app/api/router.py app/services/monitored_store_collection.py app/services/store_page_discovery.py tests/test_browser_collection.py
git commit -m "feat: fill store pools during daily collection"
```

### Task 5: Submit Manual Pages Immediately

**Files:**
- Modify: `extension/store_selection.js`
- Modify: `extension/popup/popup.js`
- Test: `tests/extension_store_selection.test.cjs`
- Test: `tests/extension_popup_store_selection.test.cjs`

**Interfaces:**
- `rankStoreSelectionCandidates(candidates, limit)` continues returning candidates ranked by page-local seven-day sum and Orders rank.
- `buildStoreSelectionPayload([page], 20)` accepts one structurally valid Orders page even when zero candidates have complete histories; the payload records candidate count, `has_next_page`, and `next_page_url`.
- Popup success text reports added/skipped counts and whether continuation was saved; failed POST retains the same page payload for safe retry.

- [ ] **Step 1: Write failing extension selection/popup tests**

Add tests that one page with fewer than 20 complete histories produces an immediate payload, a valid page with zero complete histories still produces a snapshot payload, same-page retry replaces the prior staged result rather than accumulating pages, and failed submission keeps retry data. Assert missing dates are not converted to zero and no page outside the active tab is submitted.

- [ ] **Step 2: Run the tests and confirm the expected failures**

Run: `node --test tests/extension_store_selection.test.cjs tests/extension_popup_store_selection.test.cjs`

Expected: FAIL because current selection logic waits for 20 candidates while more pages remain and requires a nonempty selected set.

- [ ] **Step 3: Implement current-page payload and popup flow**

Build the ranked selection from the current tab only. Submit immediately after a structurally valid page read, including zero eligible products; remove the multi-page local staging gate. Preserve pending payload only for an API retry, and clear it only after backend acknowledgement.

- [ ] **Step 4: Run selection and popup tests**

Run the command from Step 2. Expected: PASS; ranking, duplicate-ID handling, visible-zero retention, and failure retry behavior are covered.

- [ ] **Step 5: Commit Task 5 files only**

```bash
git add extension/store_selection.js extension/popup/popup.js tests/extension_store_selection.test.cjs tests/extension_popup_store_selection.test.cjs
git commit -m "feat: enroll eligible products per store page"
```

### Task 6: Scan One Candidate Page in the Scheduled Worker

**Files:**
- Modify: `extension/background.js`
- Modify: `extension/content.js`
- Modify: `extension/page_collector.js`
- Modify: `extension/store_selection.js`
- Modify: `extension/manifest.json`
- Modify: `tests/extension_background_worker.test.cjs`
- Modify: `tests/extension_content.test.cjs`
- Modify: `tests/extension_page_collector.test.cjs`
- Modify: `tests/extension_store_collector.test.cjs`

**Interfaces:**
- Add collector message `STORE_CANDIDATE_SCAN` with `{ targetDate, expectedStoreId, expectedPageUrl }`.
- Route it in `extension/content.js` to `AliExpressMonitorPageCollector.collectStoreCandidatePage(message)`.
- It returns `{ state: "ready", page_result: { platform_store_id, url, candidates, has_next_page, next_page_url, scan_complete, diagnostics } }`, or a typed existing challenge/login/failure state.
- `collectStorePages(tabId, item)` accepts zero or more monitored IDs, preserves the current target-ID collection, scans at most `item.discovery_page_url` once when the active pool is under 20, and includes `candidate_page` in the submitted store result.

- [ ] **Step 1: Write failing page collector and worker tests**

Add tests that candidate charts are read sequentially in DOM order, missing candidate histories are diagnostics rather than zeroes, an incomplete page adds no candidates, the worker visits only the cursor page for discovery, an already-visited cursor page is reused, an empty monitored-ID list still performs discovery, and no product-detail URL is opened. Add failure tests for wrong store, wrong sort, challenge, page timeout, and an incomplete page retaining the cursor.

- [ ] **Step 2: Run the focused Node tests and confirm expected failures**

Run: `node --test tests/extension_background_worker.test.cjs tests/extension_page_collector.test.cjs tests/extension_store_collector.test.cjs`

Expected: FAIL because the worker currently rejects empty target lists and only extracts products whose IDs are already monitored.

- [ ] **Step 3: Implement bounded one-page candidate scan**

Read the backend-provided cursor. Reuse candidate data if the cursor URL was already parsed; otherwise navigate to exactly that Orders URL after preserving existing target metrics. Process candidate cards sequentially, enforce the existing 8-minute overall work budget and 4-minute per-page response bound, and include at most 11 unique visited URLs (10 existing monitored-ID pages plus one candidate page). If the candidate page cannot be fully read, submit its partial diagnostics with `scan_complete: false`; never advance its cursor. Do not open product detail pages or change challenge handling.

- [ ] **Step 4: Bump extension version and verify the extension tests**

Bump `extension/manifest.json` and `extension/page_collector.js` from `0.6.73` to `0.6.74`; update the store-selection extractor version string to `0.6.74`. Run the command from Step 2, plus `node --check extension/background.js`, `node --check extension/page_collector.js`, and `node --check extension/popup/popup.js`.

- [ ] **Step 5: Commit Task 6 files only**

```bash
git add extension/background.js extension/content.js extension/page_collector.js extension/store_selection.js extension/manifest.json tests/extension_background_worker.test.cjs tests/extension_content.test.cjs tests/extension_page_collector.test.cjs tests/extension_store_collector.test.cjs
git commit -m "feat: scan store candidate pages in scheduled runs"
```

### Task 7: Integration Verification and Operator Notes

**Files:**
- Modify: `extension/README.md`
- Test: backend and extension suites listed below.

- [ ] **Step 1: Document the manual and scheduled behavior**

Update `extension/README.md` to state that a valid current-page submission immediately appends eligible products, the backend cursor resumes across collection computers, a daily store task reads at most one candidate page while updating current products, and a full pool pauses discovery without replacing products.

- [ ] **Step 2: Run all automated suites and syntax/whitespace checks**

Run: `.\.venv\Scripts\python.exe -m pytest --basetemp .superpowers/sdd/2026-10-12-store-page-pool-fill/pytest-tmp`

Run: `node --test tests/extension_background_worker.test.cjs tests/extension_content.test.cjs tests/extension_dashboard_bridge.test.cjs tests/extension_page_collector.test.cjs tests/extension_popup_store_selection.test.cjs tests/extension_store_collector.test.cjs tests/extension_store_selection.test.cjs`

Run: `git diff --check`

Expected: all tests pass and no whitespace errors.

- [ ] **Step 3: Commit the documentation**

```bash
git add extension/README.md
git commit -m "docs: explain pagewise store pool filling"
```

- [ ] **Step 4: Hand off for separate live acceptance**

Report test results and the un-deployed extension version. Ask the user to authorize deployment/restart/reload separately. Live acceptance must verify one underfilled store: page 1 candidates enter immediately, a later daily run scans the saved next page, only vacant slots are filled, existing IDs remain active, no detail pages open, and verification preserves the cursor. Do not start or mutate a live queue during automated verification.
