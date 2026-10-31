# Shared Browser Collection Workers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let every online browser collector claim distinct work from the daily shared queue, while keeping the queue behind a manual start and allowing one computer to drain it alone.

**Architecture:** Keep one database-backed daily queue and the existing worker IDs, claim tokens, heartbeat checks, and row locks. Change backend ownership rules so a selected computer authorizes start but does not own dispatch, and allow multiple store tasks to remain running. Add per-worker claim counts and update the dashboard copy and selector behavior; the extension claim protocol stays unchanged.

**Tech Stack:** Python, FastAPI, SQLAlchemy, Pydantic, pytest, server-rendered HTML, vanilla JavaScript, CSS, Docker Compose on NAS.

**Spec:** `docs/superpowers/specs/2026-10-31-shared-browser-collection-workers.md`

## Global Constraints

- The queue stays held until an online computer is selected and a person explicitly clicks `开始采集` or `继续采集`.
- Once dispatch is enabled, every registered worker with a heartbeat within the existing two-minute online window may claim from the shared queue.
- Preserve the existing five-minute claim lease, retry count, maximum task runtime, and claim-token fencing.
- Keep the existing limit of at most two open collection pages per computer.
- Manual pause and AliExpress verification remain queue-wide gates; an online verification owner retains its task on resume.
- Keep daily queue creation, schedule, monitored-store membership, identity safeguards, metrics, and existing queue data unchanged.
- Do not add a database migration or change the extension request protocol.
- Do not automatically start or reset a daily queue during implementation or deployment.

## Review Focus

- Missing or stale worker heartbeats must not claim new work; the selected computer must still be online for manual start. Pin with `test_start_requires_selected_online_worker` and `test_stale_or_missing_worker_cannot_claim_shared_queue` in Task 1.
- Two workers claiming while another store task is still running must receive different item IDs and tokens. Pin with `test_both_online_workers_claim_distinct_store_items` in Task 1.
- If only one worker is online, it must be able to claim each task as it becomes available until the queue is drained. Pin with `test_single_online_worker_can_drain_entire_shared_queue` in Task 1.
- Moving the selected start computer during dispatch must not pause the run or block the other worker. Pin with `test_selecting_start_worker_does_not_pause_shared_dispatch` in Task 1 and `test_dashboard_worker_switch_does_not_prompt_for_handoff` in Task 3.
- Finishing one of several store tasks must not advance the phase while another store task remains pending or running. Pin with `test_parallel_store_items_keep_store_phase_until_all_terminal` in Task 1.
- A platform-verification task must remain bound to its online browser owner, then be requeued if that owner is offline at resume. Pin with `test_verification_resume_keeps_online_owner_or_requeues_offline_owner` in Task 1.
- Worker load and start availability must reflect shared claims, not an obsolete handoff count. Pin with `test_control_reports_active_claims_per_worker` and `test_control_can_start_while_another_worker_has_a_valid_claim` in Task 2.
- Duplicate worker labels must remain distinguishable without rewriting persisted computer names. Pin with `test_control_disambiguates_duplicate_worker_labels` in Task 2.

---

### Task 1: Shared backend claim semantics

**Files:**
- Modify: `app/services/browser_collection.py`
- Test: `tests/test_browser_collection.py`

**Interfaces:**
- `claim_next_item(db: Session, worker_id: str | None) -> tuple[BrowserCollectionRun | None, BrowserCollectionItem | None]` admits a registered online requester whenever dispatch is enabled, regardless of `selected_worker_id` or other workers' running tasks.
- `select_browser_collection_worker(db: Session, worker_id: str, *, confirm_handoff: bool = False) -> dict` changes the computer required for a future manual start without closing the dispatch gate; retain the optional argument for older dashboard clients and ignore it.
- `start_browser_collection_run(db: Session, run: BrowserCollectionRun | None = None) -> BrowserCollectionRun` still requires the selected computer to be online, but does not wait for claims from other workers to drain.
- [x] **Step 1: Add failing multi-worker claim tests**

Add `test_both_online_workers_claim_distinct_store_items`, `test_one_worker_can_claim_multiple_store_items`, `test_single_online_worker_can_drain_entire_shared_queue`, `test_selected_worker_offline_after_start_does_not_block_other_online_claims`, `test_stale_or_missing_worker_cannot_claim_shared_queue`, and `test_expired_claim_can_be_reclaimed_by_another_online_worker`. With dispatch started by a selected online computer, assert that both registered online workers can claim separate store items, a single worker can claim another store while its earlier task remains `RUNNING`, one worker can finish and claim all queued stores in turn, an already-started run continues when the selected worker goes offline, stale or absent worker identity gets no item, and an expired item's old token cannot write after a second worker reclaims it.

- [x] **Step 2: Add failing run-control and phase tests**

Add `test_selecting_start_worker_does_not_pause_shared_dispatch`, `test_start_does_not_wait_for_other_worker_claims`, `test_parallel_store_items_keep_store_phase_until_all_terminal`, and `test_verification_resume_keeps_online_owner_or_requeues_offline_owner`. Assert selection leaves `dispatch_enabled` true, start/resume succeeds with another worker's valid running claim, the run stays in `STORE_DISCOVERY` until all store items are terminal, and verification resumes with the original online worker or returns to `PENDING` if it is offline.

- [x] **Step 3: Run the focused backend tests and confirm the new tests fail for the old behavior**

Run: `pytest tests/test_browser_collection.py -q`

Expected: the new shared-claim and selection tests fail because claims are restricted to the selected worker, concurrent store tasks are blocked, and selector changes begin a handoff.

- [x] **Step 4: Rewrite existing ownership tests to the shared-queue contract**

Before changing production code, rewrite the existing exclusive-ownership cases as `test_any_online_worker_can_claim_shared_queue`, `test_selected_worker_offline_does_not_block_shared_claims`, `test_selecting_start_worker_keeps_dispatch_open_with_old_claims`, `test_selection_preserves_active_claim_and_rejects_stale_result`, `test_concurrent_store_claim_is_allowed_while_current_store_runs`, and `test_store_discovery_claim_allows_multiple_running_stores_per_run`. Keep stale-token rejection assertions while changing ownership and parallel-claim assertions to the approved shared-queue contract.

Run: `pytest tests/test_browser_collection.py -q`

Expected: the suite remains red only on the not-yet-implemented shared claim, selector, start, or verification-resume behavior; test setup and assertions themselves run without errors.

- [x] **Step 5: Implement shared worker eligibility, start, and selection behavior**

In `claim_next_item`, require a registered worker with a heartbeat no older than two minutes, enabled dispatch, and the existing row locks. Remove the selected-worker comparison, foreign-running-item guard, and one-running-store guard. Keep the run lock around claim assignment and keep unique worker IDs and random claim tokens on every item. In `start_browser_collection_run`, retain the selected-online prerequisite but remove the foreign-claim drain rejection. In `select_browser_collection_worker`, update the selected worker and label without setting `dispatch_enabled = False`; keep the legacy `confirm_handoff` parameter accepted.

- [x] **Step 6: Implement queue-wide verification resume ownership**

In `start_browser_collection_run`, retain the challenge claim for its original worker if that worker is online; if the original worker is offline, clear its claim and return the item to `PENDING` for shared dispatch. Preserve the queue-wide verification gate.

- [x] **Step 7: Run backend tests and check the acceptance cases**

Run: `pytest tests/test_browser_collection.py -q`

Expected: all tests pass, including the rewritten ownership and handoff tests; stale-token rejection and queue-wide pause behavior remain intact, and distinct workers can hold distinct `RUNNING` store items at once.

- [x] **Step 8: Commit the backend change**

```bash
git add app/services/browser_collection.py tests/test_browser_collection.py
git commit -m "feat: share browser collection claims across workers"
```

### Task 2: Worker claim counts in the control API

**Files:**
- Modify: `app/services/browser_collection.py`
- Modify: `app/api/schemas.py`
- Test: `tests/test_browser_collection.py`

**Interfaces:**
- `BrowserCollectionWorkerRead` adds `active_claim_count: int = 0`.
- `BrowserCollectionWorkerRead` adds `display_label: str`; use the stored/selected-overridden label when unique, otherwise append ` · <first 8 worker ID characters>`.
- Count `RUNNING` and `NEEDS_VERIFICATION` items assigned to the worker with an unexpired lease.
- Keep `handoff_pending` and `draining_count` in the control response for compatibility, but return `false` and `0`.
- Do not include foreign-worker claims in `can_start`; preserve the selected-online and remaining-work requirements.

- [x] **Step 1: Add the failing control-count test**

Add `test_control_reports_active_claims_per_worker`, `test_control_can_start_while_another_worker_has_a_valid_claim`, and `test_control_disambiguates_duplicate_worker_labels`. Claim tasks as two workers and assert each worker's response count matches its own unexpired claimed items, a worker with none reports zero, a valid claim does not block a manual start by the selected online computer, and equal stored labels produce distinct display labels without changing the persisted labels.

- [x] **Step 2: Run the focused control-count test and confirm it fails**

Run: `pytest tests/test_browser_collection.py::test_control_reports_active_claims_per_worker tests/test_browser_collection.py::test_control_can_start_while_another_worker_has_a_valid_claim tests/test_browser_collection.py::test_control_disambiguates_duplicate_worker_labels -q`

Expected: FAIL because worker records do not yet expose active claim counts and `can_start` still depends on the old drain count.

- [x] **Step 3: Implement computed worker counts**

Compute counts and display labels in `browser_collection_control_status` without storing duplicate counters or names. Add `active_claim_count` and `display_label` to `BrowserCollectionWorkerRead`, keep the selected-label override, append the worker ID prefix only when labels collide, return compatibility handoff fields as `false` and `0`, and remove the old drain condition from `can_start`. Do not add a database migration.

- [x] **Step 4: Run the backend control tests**

Run: `pytest tests/test_browser_collection.py -q`

Expected: PASS, including selected-worker start prerequisites and per-worker active counts.

- [x] **Step 5: Commit the control API change**

```bash
git add app/services/browser_collection.py app/api/schemas.py tests/test_browser_collection.py
git commit -m "feat: report shared browser worker load"
```

### Task 3: Shared-worker dashboard

**Files:**
- Modify: `app/templates/dashboard.html`
- Modify: `app/templates/base.html`
- Modify: `app/static/dashboard.js`
- Modify: `app/static/dashboard.css`
- Test: `tests/test_dashboard.py`

**Interfaces:**
- Consume `BrowserCollectionControlRead.workers[*].active_claim_count` from Task 2.
- Keep the existing worker selector, separate start/continue button, and `/api/browser-collection/control/worker` endpoint.

- [x] **Step 1: Add failing dashboard assertions**

Add `test_dashboard_worker_switch_does_not_prompt_for_handoff` and `test_dashboard_shows_shared_claim_counts`. Assert the rendered panel explains that all online computers can claim after start, renders the server-provided disambiguated `display_label` and worker-load count, exposes a worker-load element, and the selection handler sends the new worker without showing a handoff confirmation or claiming that selection pauses dispatch. Update the static asset version assertion for the changed dashboard assets.

- [x] **Step 2: Run the focused dashboard tests and confirm they fail**

Run: `pytest tests/test_dashboard.py -q`

Expected: the new tests fail against the exclusive-handoff text and selector handler.

- [x] **Step 3: Update the dashboard selection and worker-load display**

In `dashboard.html`, replace exclusive-owner and drain messaging with start-authorization and shared-queue guidance, and add an element for worker loads. Use `display_label` in both the server-rendered selector and dynamic worker list. In `dashboard.js`, render each computer's online state and `active_claim_count`, remove the active-run confirmation, and keep selection changes separate from the start endpoint. Preserve the disabled start state when the selected computer is offline. Add only the CSS needed to keep the worker-load list readable at desktop and mobile widths. In `base.html`, update the dashboard asset query version so browsers fetch the new JavaScript and CSS.

- [ ] **Step 4: Run dashboard and related browser-collection tests**

Run: `pytest tests/test_dashboard.py tests/test_browser_collection.py -q`

Expected: both suites pass, the manual start gate remains intact, and switching the selected computer does not alter dispatch.

- [x] **Step 5: Commit the dashboard change**

```bash
git add app/templates/dashboard.html app/templates/base.html app/static/dashboard.js app/static/dashboard.css tests/test_dashboard.py
git commit -m "feat: show shared browser worker status"
```

### Task 4: NAS rollout and two-computer acceptance

**Files:**
- No source files. Deployment and runtime state only.

**Interfaces:**
- Deploy the app version produced by Tasks 1, 2, and 3; no database migration and no extension protocol change are expected.
- Read current NAS image, service, database revision, today's run, queue dispatch state, active claims, and worker heartbeats before changing the service.

- [ ] **Step 1: Record read-only NAS preflight evidence**

Confirm the target app and branch/digest, current database head, current run status, pending/running item IDs, dispatch flag, and collector worker IDs/heartbeat ages. Do not deploy while dispatch is enabled, the run status is `RUNNING` or `NEEDS_VERIFICATION`, or any item is `RUNNING` or `NEEDS_VERIFICATION`; do not pause a live queue to make deployment possible.

- [ ] **Step 2: Deploy only the backend service**

Use the existing approved `monitor-nas` workflow to build and deploy Compose service `backend` only. Leave `frontend`, `scheduler`, `postgres`, dependencies, and persistent volumes untouched. Do not migrate, reset the queue, change monitored-store membership, or start/resume collection. Verify the deployed app revision and `/api/browser-collection/control` response.

- [ ] **Step 3: Confirm both collector computers are online**

Verify two distinct worker IDs have fresh heartbeats and the supported existing extension version. No extension reload is required unless a worker fails to advertise its ID or heartbeat after the app deploy.

- [ ] **Step 4: Complete controlled two-computer live acceptance**

After the user explicitly starts the held queue from the dashboard, observe at least two store tasks with status `RUNNING` at the same time, assigned to distinct worker IDs and distinct item IDs. Confirm each computer shows its current claimed-task count and the run remains in store discovery. After the concurrent tasks finish, confirm one online computer can claim another pending task by itself. Do not force an in-flight page to fail just to test lease recovery; the backend test suite covers expired-lease reassignment. Report this real-page evidence separately from automated test results.

- [ ] **Step 5: Preserve or pause the queue at the user-directed boundary**

Do not cancel, reset, or force-complete work. Leave normal dispatch behavior as configured by the user and report any platform challenge or login requirement for the user to handle in the browser.

## Implementation Method

Use native execution in this session. The backend, API response, and dashboard changes share interfaces and should be reviewed together; multi-agent execution is unavailable in the current collaboration mode. The user approved the plan before implementation.
