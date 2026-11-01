# 主采集电脑与辅助采集电脑实施计划

> **For agentic workers:** Use the `executing-plans` skill to implement this plan task by task.

**Goal:** Bind each daily browser-collection run to one locked primary worker and an explicit set of helper workers, so only those workers can claim new tasks.

**Architecture:** Persist the primary worker ID and helper IDs on `BrowserCollectionRun`. Keep the existing global selected-worker control as the default primary for a new or still-unassigned run. Assign roles transactionally when a run first starts (or when a legacy run first resumes), enforce membership in `claim_next_item`, and return role IDs through the existing run API. Preserve existing claim owners, tokens, leases, heartbeat behavior, and row locking. Add primary/helper controls to the dashboard without changing the extension protocol.

**Tech Stack:** Python, FastAPI, SQLAlchemy, Alembic, Pydantic, pytest, server-rendered HTML, vanilla JavaScript, CSS, Docker Compose on NAS.

**Spec:** `docs/superpowers/specs/2026-11-01-primary-and-helper-browser-collection-design.md`

## Global Constraints

- The selected primary must be online when a new run starts. A new run's primary and helper IDs are saved in the same transaction that enables dispatch.
- A run's primary and helper membership becomes immutable after assignment. Changing the dashboard's selected primary afterward only sets the default for a later run.
- Multiple helpers may be selected. A helper may be offline when selected; it cannot claim until its heartbeat is fresh. The primary cannot also be a helper.
- Only the primary and saved helpers may receive new claims. An unselected online worker continues to heartbeat but receives no task or claim token.
- Do not invalidate or transfer an existing valid claim when assigning roles. Preserve its worker ID, token, and lease; normal result/token checks remain authoritative. Once a claim expires and is returned to `PENDING`, normal run membership applies.
- A legacy run without stored roles gets one assignment on its first start/resume under the new behavior. Existing items and claims are not rewritten by that assignment.
- Resuming a run with roles requires at least one authorized worker online. An already claimed task may still be completed by its recorded owner under existing ownership validation.
- The migration is additive: do not alter run/item status, dispatch flags, owners, claim tokens, leases, or collected data.
- Do not automatically start, resume, pause, cancel, clear, reset, or reorder the live queue. Do not change the extension protocol, store identity, monitoring pool, Dianxiaomi flow, or per-computer two-page limit.
- Do not deploy or run the migration on NAS without fresh read-only queue preflight and separate confirmation for this specific backend deployment/migration. Do not deploy while any item is `RUNNING` or `NEEDS_VERIFICATION`; do not pause work merely to satisfy the gate.
- Report automated source verification, NAS deployment/migration, queue state, and user-controlled live acceptance as separate evidence.

## Review Focus

- Primary and helper IDs must be stored on the run, not inferred from mutable global selection or labels.
- `test_unselected_online_worker_cannot_claim_new_item` must prove an online third computer receives neither an item nor a claim token.
- `test_existing_claim_owner_can_finish_after_run_role_assignment` must prove role assignment does not strand a valid existing claim.
- `test_expired_claim_can_only_be_reclaimed_by_run_member` must prove a returned pending item cannot be claimed by an unselected worker.
- `test_legacy_resume_assigns_roles_without_changing_existing_claim` must prove one-time legacy assignment retains the existing worker/token relationship.
- The dashboard must distinguish the current run's locked roles from the global primary choice used for a future run.
- The NAS migration must preserve current queue rows and must not start dispatch.

---

### Task 1: Persist run roles and expose the API contract

**Files:**
- Modify: `app/db/models.py`
- Create: `alembic/versions/20261101_0019_browser_collection_run_workers.py`
- Modify: `app/api/schemas.py`
- Modify: `app/services/browser_collection.py`
- Test: `tests/test_schema.py`
- Test: `tests/test_browser_collection.py`

**Interfaces:**
- `BrowserCollectionRun.primary_worker_id: str | None` references `browser_collection_workers.worker_id`.
- `browser_collection_run_helpers` stores unique `(run_id, worker_id)` pairs with foreign keys to runs and workers; ORM relationships expose the helper workers.
- `BrowserCollectionRunRead.primary_worker_id` is nullable and `helper_worker_ids` is always a list.
- Add start/resume request schemas carrying `helper_worker_ids`; allow the body to be omitted for older callers. A missing helper list on an unassigned legacy run means an empty helper list.
- `serialize_run(run)` includes the saved primary/helper IDs.

- [ ] **Step 1: Pin the response and persistence contract**

Add schema/service tests asserting an unassigned run serializes as `primary_worker_id is None` and `helper_worker_ids == []`, and an assigned run returns its persisted worker IDs regardless of a later change to `BrowserCollectionControl.selected_worker_id`. Add model relationship coverage for multiple helpers and duplicate-association rejection.

- [ ] **Step 2: Add the additive model and migration**

Add nullable `primary_worker_id` and the helper association table. Create revision `20261101_0019`, down revision `20261031_0018`; add only these structures and implement a matching downgrade. Leave all existing run and item fields untouched.

- [ ] **Step 3: Add run API schemas and serialization**

Define request models for `start-today` and `/{run_id}/resume`, with optional request bodies for compatibility. Add `primary_worker_id` and `helper_worker_ids` to `BrowserCollectionRunRead` and `serialize_run`. Keep control selection APIs and worker heartbeat payloads backward compatible.

- [ ] **Step 4: Review and verify the persistence boundary**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_schema.py tests/test_browser_collection.py -q`

Inspect the migration revision chain and its SQLAlchemy/Alembic operations. Confirm that preexisting runs have a null primary and no helper rows, and that the migration does not update any queue item or dispatch state.

### Task 2: Lock run roles and enforce claim eligibility

**Files:**
- Modify: `app/services/browser_collection.py`
- Modify: `app/api/router.py`
- Modify: `app/api/schemas.py`
- Test: `tests/test_browser_collection.py`

**Interfaces:**
- `start_browser_collection_run(db, run=None, helper_worker_ids=None)` assigns roles only if the run has none; it uses the selected online worker as primary.
- `resume_run(db, run, helper_worker_ids=None)` assigns roles once only for an unassigned legacy run; otherwise it preserves the saved roles and rejects attempted substitutions.
- `claim_next_item(db, worker_id)` returns no item and no token when the requester is not the saved primary/helper, even if the requester is online.

- [ ] **Step 1: Add failing run-role tests**

Cover initial primary assignment, multiple helpers, rejection of unknown/duplicate/helper-equals-primary IDs, lock after start, and a later global primary selection not changing the active run. Include a legacy run with no roles and verify its first resume assigns the selected primary plus submitted helpers exactly once.

- [ ] **Step 2: Add failing ownership and claim tests**

Add `test_unselected_online_worker_cannot_claim_new_item`, `test_multiple_helpers_claim_distinct_items`, `test_expired_claim_can_only_be_reclaimed_by_run_member`, `test_existing_claim_owner_can_finish_after_run_role_assignment`, and `test_resume_requires_an_authorized_worker_online`. Assert unselected workers get neither an item nor a token; valid current claims retain the same owner/token; expired items return to the queue and only an authorized worker can reclaim them.

- [ ] **Step 3: Assign and lock roles in the start/resume transaction**

Validate worker IDs against registered records, reject duplicate IDs and primary/helper overlap, and allow offline helpers to remain assigned. Require an online selected primary on first assignment. Save the role set before enabling dispatch in the same transaction. For a role-locked run, preserve its saved roles and reject a different submitted helper list; do not let current global selection replace its primary.

- [ ] **Step 4: Gate only new claims by run membership**

In `claim_next_item`, after validating the worker heartbeat and run state, check membership before assigning a pending item. Leave `finish_item`, challenge/result submission, heartbeat lease renewal, row locks, and claim-token fencing based on the item's recorded owner unchanged. Ensure expired claims are cleared using existing logic and become claimable only by current run members.

- [ ] **Step 5: Resume based on authorized online workers and verify backend behavior**

For a role-locked run, allow resume when the primary or at least one helper is online; fail closed with dispatch disabled when none are online. Preserve the current online challenge owner and existing claim credentials, including when that owner is no longer a run member. Requeue an expired/offline claim only through the existing lease path.

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_browser_collection.py -q`

### Task 3: Add primary/helper controls to the dashboard

**Files:**
- Modify: `app/templates/dashboard.html`
- Modify: `app/static/dashboard.js`
- Modify: `app/static/dashboard.css`
- Modify: `app/services/browser_collection.py`
- Test: `tests/test_dashboard.py`
- Test: `tests/test_browser_collection.py`

- [ ] **Step 1: Add dashboard behavior assertions**

Assert the page has a primary selector and a multiple helper selector, helper choices exclude the selected primary, offline helpers are visibly marked, locked current-run roles are rendered separately from the next-run primary choice, and the start/resume request sends the helper IDs only when a run has not yet locked roles.

- [ ] **Step 2: Render primary, helper, and active-run states**

Rename the existing selector to “主采集电脑” and add “辅助采集电脑” as a multi-select. Show the active run's saved primary and helper set as locked. Label the global selected primary as the default for the next run after current roles lock. Keep offline helpers visible and explain that they cannot claim until their heartbeat is fresh.

- [ ] **Step 3: Wire role selection and start/resume payloads**

Update `updateBrowserCollectionControl` and the run start handler in `dashboard.js` to exclude the chosen primary from helper options, preserve pending helper choices across control refreshes, send `helper_worker_ids` to first start or first legacy resume, and avoid sending a replacement role list for an already locked run. Keep selector changes from starting or resuming the queue.

- [ ] **Step 4: Reflect run-aware start availability and verify UI behavior**

Update the control status calculation so a locked run can resume when any authorized worker is online, even if the globally selected next-run primary is offline. Keep first start disabled unless its selected primary is online. Update help text to state that only the saved primary and enabled helpers can claim new tasks.

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_dashboard.py tests/test_browser_collection.py -q`

### Task 4: Complete verification and gated NAS rollout

**Files:**
- Modify as needed: `tests/test_browser_collection.py`, `tests/test_dashboard.py`, `tests/test_schema.py`
- Reference: `docs/nas-monitor-deploy-operations.md`

- [ ] **Step 1: Run the focused and full source checks**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_browser_collection.py tests/test_dashboard.py tests/test_schema.py -q
.\.venv\Scripts\python.exe -m pytest
git diff --check
```

Confirm the extension protocol remains unchanged, run-role reads are stable, the main/helper/unselected cases pass, and no test starts a live queue.

- [ ] **Step 2: Recheck NAS and deployment preconditions**

Using the documented read-only commands, record the NAS app/revision, Alembic head, latest run, dispatch flag, item counts/statuses, and worker heartbeat IDs. Do not proceed if any queue item is `RUNNING` or `NEEDS_VERIFICATION`; do not pause/resume the queue to create a deployment window. If the gate is not met, leave the migration undeployed and report the blocking item state.

- [ ] **Step 3: Obtain specific authorization and deploy backend only**

After the queue gate is clear, request/confirm authorization for this backend rebuild and Alembic migration. Deploy only Compose service `backend` using the command in `docs/nas-monitor-deploy-operations.md`; do not rebuild frontend/scheduler, touch volumes, or start/resume collection. The backend startup migration advances the database to `20261101_0019`.

- [ ] **Step 4: Verify deployment and preserve queue state**

Read back backend health, application revision, Alembic head, `/api/browser-collection/control`, and the latest run/items. Confirm role fields are present, existing queue counts/statuses/owners/leases are unchanged, and dispatch remains as it was before deployment. Report deployment evidence separately from source tests.

- [ ] **Step 5: Perform user-controlled live acceptance**

Do not start or resume the queue automatically. Once the user confirms the live queue is ready, have them choose the intended primary and one or more helpers and click the existing manual start/resume control. Read-only verify that each enabled online worker receives a different item and that an online unselected computer receives no new claim. Report this acceptance separately from automated tests and deployment health.
