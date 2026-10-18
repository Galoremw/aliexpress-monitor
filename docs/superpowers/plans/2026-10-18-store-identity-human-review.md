# Store Identity Human Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a human resolve one ambiguous AliExpress store task, resume that task, and retain a store-scoped URL identity that both collection computers can validate later.

**Architecture:** The backend owns pending identity evidence and approved URL identities. The extension submits structured evidence, uses only backend-approved aliases for later collection, and keeps platform challenges on their existing path. The dashboard offers task-scoped confirm/reject actions; confirmation records the alias and resumes normal queue dispatch, while rejection fails only that task and leaves the run paused.

**Tech Stack:** Python 3, FastAPI, SQLAlchemy, Alembic, pytest, Chrome Manifest V3 extension JavaScript, vanilla dashboard JavaScript/CSS.

**Spec:** `docs/superpowers/specs/2026-10-18-store-identity-human-review-design.md`

## Global Constraints

- A human approval applies to one monitored store and one normalized URL identity; it is not a global ignore flag.
- Compare AliExpress host plus `/store/{route_store_id}/pages/all-items.html`; ignore query parameters such as page, sort, tracking, and `spm`.
- Keep the canonical `Store.aliexpress_store_id` and `Store.url` unchanged.
- A route ID already bound to another monitored store cannot be silently reassigned.
- A later conflicting explicit Store ID or inconsistent available store-name evidence returns to human review.
- Identity review and AliExpress login/CAPTCHA/platform verification are separate states; never bypass platform verification.
- `确定无误` records the reviewed URL identity and resumes normal queue dispatch; `错误` records no alias and keeps the queue paused.
- The backend is authoritative so approved identities are shared across both computers.
- Do not deploy, run a live migration, restart services, reload extensions, or change the live queue as part of implementation without separate authorization.

## Review Focus

- Malformed, non-HTTPS, off-domain, or non-Orders URLs must not become identities. Test in Task 1 with `test_normalize_store_orders_identity_rejects_unsafe_urls`.
- An approved route with conflicting explicit Store ID or store-name evidence must pause for review. Test in Task 3 with `test_approved_route_alias_still_rejects_conflicting_page_evidence`.
- A route ID already assigned to another store must not be overwritten; repeated confirmation for the same store/route must be idempotent. Test in Task 2 with `test_identity_confirmation_is_idempotent_and_rejects_cross_store_collision`.
- Stale tasks, invalid claims, or a changed current store must not approve or resume another task. Test in Task 2 with `test_identity_decision_rejects_stale_or_unclaimed_item`.
- Real AliExpress/IXSPY challenges must retain their existing pause behavior and never show identity decisions. Test in Tasks 2 and 4 with `test_platform_challenge_cannot_use_identity_decisions` and `test_identity_buttons_render_only_for_identity_review_items`.

---

### Task 1: Persist Store-Scoped URL Identities

**Files:**
- Modify: `app/db/models.py`
- Create: `alembic/versions/20261018_0014_store_url_identities.py` (down revision `20261016_0013`)
- Create: `app/services/store_identity.py`
- Test: `tests/test_store_identity.py`

**Interfaces:**
- Produces `normalize_store_orders_identity(page_url: str) -> tuple[str, str]`, returning `(route_store_id, normalized_page_identity)` or raising `ValueError` for an invalid URL. Normalized identity is lowercase host plus the exact Orders path; query and fragment are excluded.
- Produces `StoreUrlIdentity` with `store_id`, globally unique `route_store_id`, unique `normalized_page_identity`, `first_observed_url`, JSON `evidence`, nullable `source_item_id`, and `approved_at`.
- Adds nullable `identity_review_url` and JSON `identity_review_evidence` to `BrowserCollectionItem` for pending review evidence; these fields do not alter task status by themselves.

- [ ] **Step 1: Write failing normalization and persistence tests**

Add tests for query-insensitive normalization, host case normalization, preserving distinct route IDs, rejecting malformed/off-domain/non-HTTPS/wrong-path URLs, and enforcing one store per route identity. Include a test that pending evidence can be stored on a collection item without changing canonical store fields.

- [ ] **Step 2: Run the focused tests and verify they fail**

Run: `pytest tests/test_store_identity.py -q`
Expected: FAIL because the normalizer and identity model do not exist.

- [ ] **Step 3: Add the model, migration, and normalizer**

Create `StoreUrlIdentity` with the constraints above. Add the two nullable review-evidence columns to `BrowserCollectionItem`. Create Alembic revision `20261018_0014` based on `20261016_0013`; its downgrade drops the new table and columns. Implement the pure URL normalizer using `urllib.parse`, accepting only HTTPS AliExpress hosts and the exact numeric store Orders path.

- [ ] **Step 4: Run the focused tests and verify they pass**

Run: `pytest tests/test_store_identity.py -q`
Expected: PASS, including model-level uniqueness and nullable-evidence behavior.

- [ ] **Step 5: Commit this task only**

Stage only the four files listed in this task and commit as `feat: persist approved store URL identities`.

### Task 2: Add Identity Review State Transitions and API

**Files:**
- Modify: `app/api/schemas.py`
- Modify: `app/api/router.py`
- Modify: `app/services/browser_collection.py`
- Modify: `app/services/store_identity.py`
- Test: `tests/test_browser_collection.py`

**Interfaces:**
- Request `BrowserCollectionIdentityReviewRequest` contains `worker_id`, `claim_token`, `page_url`, `identity_error_type` (`store_identity_mismatch` or `store_identity_unverified`), `error_message`, and structured `evidence` (`route_store_id`, `heading_store_id`, `labeled_store_ids`, `page_title`, nullable `ixspy_store_name`, and `identity_method`). Persist the queue item's distinct `error_type="store_identity_review"` and retain `identity_error_type` in its evidence.
- Add `record_store_identity_review(db, item, page_url, error_type, error_message, evidence) -> BrowserCollectionRun`, `confirm_store_identity_review(db, item_id) -> BrowserCollectionRun`, and `reject_store_identity_review(db, item_id) -> BrowserCollectionRun` in `app/services/browser_collection.py`.
- Add extension endpoint `POST /api/browser-collection/items/{item_id}/identity-review` and dashboard endpoints `POST .../identity-review/confirm` and `POST .../identity-review/reject`; each returns `BrowserCollectionRunRead`.
- Add `identity_review_url`, `identity_review_evidence`, and `identity_review_pending` to `BrowserCollectionItemRead` serialization.

- [ ] **Step 1: Write failing backend transition tests**

Add `test_store_identity_review_pauses_with_structured_evidence`, `test_confirm_store_identity_review_records_alias_and_resumes_current_item`, `test_reject_store_identity_review_fails_item_without_alias_and_keeps_run_paused`, `test_identity_confirmation_is_idempotent_and_rejects_cross_store_collision`, `test_identity_decision_rejects_stale_or_unclaimed_item`, `test_identity_review_rejects_url_evidence_mismatch`, `test_confirmation_requires_online_selected_worker_without_partial_mutation`, and `test_platform_challenge_cannot_use_identity_decisions`. Assert exact item/run statuses, dispatch state, alias count, route/store binding, preserved evidence, and HTTP 409 for stale or invalid decisions.

- [ ] **Step 2: Run the focused tests and verify they fail**

Run: `pytest tests/test_browser_collection.py -k 'identity_review or identity_confirmation or identity_decision or platform_challenge_cannot_use_identity_decisions' -q`
Expected: FAIL because the identity-review route and transitions do not exist.

- [ ] **Step 3: Implement transactional review transitions and routes**

Record pending evidence only for the active claimed `STORE` item and pause the run with `paused_reason="store_identity_review"`, keeping platform challenge as `platform_challenge`. Confirm under row locks that the item is still the current identity-review item, normalize its recorded URL, verify the extracted route ID equals the route ID in the stored extension evidence, and validate the route is not bound to another store. Insert the alias idempotently, retaining evidence/source task/time. Resume the same active claim when it still belongs to the selected online worker; otherwise, if a different selected worker is online, release the claim and return the item to `PENDING` for that worker. If no selected worker is online, return HTTP 409 without saving the alias or changing the paused task. Set normal queue dispatch enabled only after the alias transaction succeeds. Reject only the current item as `FAILED`, save no alias, and set the run to paused with dispatch disabled.

- [ ] **Step 4: Run the focused tests and verify they pass**

Run: `pytest tests/test_browser_collection.py -k 'identity_review or identity_confirmation or identity_decision or platform_challenge_cannot_use_identity_decisions' -q`
Expected: PASS; challenge items remain ineligible for either identity decision.

- [ ] **Step 5: Commit this task only**

Stage only the five files listed in this task and commit as `feat: add store identity review decisions`.

### Task 3: Submit Identity Evidence and Enforce Approved Aliases in the Extension

**Files:**
- Modify: `extension/background.js`
- Modify: `extension/page_collector.js`
- Modify: `extension/content.js`
- Modify: `tests/extension_background_worker.test.cjs`
- Modify: `tests/extension_store_collector.test.cjs`

**Interfaces:**
- Extend claimed/serialized store items with `approved_store_url_identities`, each containing `route_store_id`, `normalized_page_identity`, and saved evidence; include aliases only for that item's monitored store.
- Extend `sendCollectMessage` store context with `approvedStoreUrlIdentities`; pass it to the page collector as `approved_store_url_identities`.
- Extend `waitForStorePage(tabId, expectedUrl, timeoutMs, expectedStoreId, approvedStoreUrlIdentities)` to validate route aliases against the backend list.
- Identity failures return `state: "identity_review"` plus structured evidence; AliExpress challenge continues to return `state: "challenge"`.

- [ ] **Step 1: Write failing extension tests**

Add `test_identity_review_reports_actual_url_and_structured_evidence`, `test_backend_approved_alias_is_accepted_only_for_its_store`, `test_approved_route_alias_still_rejects_conflicting_page_evidence`, `test_unapproved_route_alias_stops_before_product_collection`, and `test_confirmed_identity_refreshes_alias_and_resumes_same_task`. Include a visible IXSPY store-overview name fixture; assert the evidence includes the name when uniquely readable and `null` when absent.

- [ ] **Step 2: Run the focused Node tests and verify they fail**

Run: `node --test tests/extension_store_collector.test.cjs tests/extension_background_worker.test.cjs`
Expected: FAIL on the new identity-review evidence and backend-alias cases.

- [ ] **Step 3: Implement structured evidence, alias validation, and resume synchronization**

Return page URL, route ID, heading/labeled Store IDs, title, optional uniquely readable IXSPY store-overview name, and identity method on mismatch/unverified results. On either identity result, call the new identity-review endpoint instead of the platform challenge endpoint. Allow a route alias only when its normalized identity is present in the current item's backend-provided alias list and no available explicit ID/name evidence conflicts with that store. For saved and current store names that are both present, compare NFKC-normalized, trimmed, whitespace-collapsed, case-folded values; a mismatch returns to review. Preserve current explicit matching Store ID verification and keep all unapproved mismatches blocked. Do not let locally cached `progress.route_store_id` alone authorize an alias; local progress remains usable for page cursor/dedup only. When the backend confirms a task, refresh the paused worker's local item from the returned run data before retrying identity validation on the same tab.

- [ ] **Step 4: Run the focused Node tests and verify they pass**

Run: `node --test tests/extension_store_collector.test.cjs tests/extension_background_worker.test.cjs`
Expected: PASS; unapproved aliases and conflicting evidence never reach chart collection, while platform challenge behavior remains unchanged.

- [ ] **Step 5: Commit this task only**

Stage only the five files listed in this task and commit as `feat: validate store identity aliases in extension`.

### Task 4: Add Human Decisions to the Monitoring Dashboard

**Files:**
- Modify: `app/static/dashboard.js`
- Modify: `app/static/dashboard.css`
- Modify: `tests/test_dashboard.py`
- Modify: `tests/test_browser_collection.py`

**Interfaces:**
- Identity-review queue cards render `确定无误` and `错误` only when `status === "NEEDS_VERIFICATION"` and `identity_review_pending === true`.
- Confirm/reject buttons call the corresponding item decision endpoint and apply the returned run through `updateBrowserCollection`.

- [ ] **Step 1: Write failing dashboard assertions**

Add `test_identity_review_item_exposes_review_evidence_to_dashboard` and `test_identity_buttons_render_only_for_identity_review_items`. Assert platform challenge items receive no identity actions, the observed URL/evidence are visible, action labels are exact, and the CSS sets green/red backgrounds with black text.

- [ ] **Step 2: Run the focused dashboard tests and verify they fail**

Run: `pytest tests/test_dashboard.py -k 'identity_review or identity_buttons' -q`
Expected: FAIL because the review-specific controls and styling are absent.

- [ ] **Step 3: Implement the two queue actions and feedback states**

Render the observed URL and diagnostic evidence on identity-review cards. Add green-background/black-text `确定无误` and red-background/black-text `错误` buttons with disabled/loading states. Confirm updates the queue and reports that normal collection resumed. Reject updates the queue and reports that the current task was rejected while the queue remains paused. Keep platform challenge instructions and controls separate.

- [ ] **Step 4: Run the focused dashboard tests and verify they pass**

Run: `pytest tests/test_dashboard.py -k 'identity_review or identity_buttons' -q`
Expected: PASS, including challenge cards without identity actions.

- [ ] **Step 5: Run the complete regression suites**

Run: `pytest tests/test_store_identity.py tests/test_browser_collection.py tests/test_dashboard.py -q`
Expected: PASS.

Run: `node --test tests/extension_store_collector.test.cjs tests/extension_background_worker.test.cjs`
Expected: PASS.

- [ ] **Step 6: Commit this task only**

Stage only the four files listed in this task and commit as `feat: add store identity review controls`.

## Implementation Boundary

This plan covers source changes and automated tests only. Live Alembic migration, backend rebuild/restart, extension copying/reload, queue resume, and second-computer acceptance remain separate operational steps requiring their own explicit authorization. The existing unrelated modified and untracked workspace files must remain untouched and unstaged.
