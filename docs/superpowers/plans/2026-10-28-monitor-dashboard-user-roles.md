# Monitoring Dashboard User Roles Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an administrator-managed operator account that shares monitoring data and normal workflow access but cannot remove monitored data or cancel the remaining collection queue.

**Architecture:** Add `admin` and `operator` roles to database users and preserve the configured administrator as the sole admin. Enforce restricted actions in FastAPI dependencies and reflect the same capability boundary in Jinja pages and dynamic dashboard controls. Add admin-only account lifecycle endpoints and a forced first-login password change flow.

**Tech Stack:** FastAPI, SQLAlchemy, Alembic, Pydantic, Jinja2, vanilla JavaScript, PostgreSQL.

**Spec:** `docs/superpowers/specs/2026-10-28-monitor-dashboard-user-roles-design.md`

## Global Constraints

- All accounts share the same monitoring stores, products, snapshots, metrics, and collection queues.
- The existing `AUTH_ADMIN_USERNAME` remains the only administrator; the UI only creates `operator` accounts.
- Operators may view, add, edit, collect, review identity, and perform ordinary queue actions.
- Operators may not deactivate stores or products, clear the product pool, delete Dianxiaomi failed records, cancel the remaining browser collection queue, or create, start, cancel, or retry pool replacement operations.
- Restricted operations must return `403` from the backend even when called without the dashboard UI.
- Passwords are hashed with the existing scrypt scheme; initial and reset passwords are returned once and are never sent through chat.
- Account disable and password reset revoke the user's existing sessions; account rows remain in the database.
- Migrations are additive and preserve existing monitoring and queue records.
- Do not deploy or restart NAS services as part of implementation.
- Do not add or run automated tests unless the user separately requests testing or verification; perform only the implementation review explicitly requested for the selected task.

## Review Focus

- Existing configured admin after migration: startup bootstrap must retain admin access without promoting any operator.
- Direct status mutation: `PATCH /api/stores/{store_id}` and `PATCH /api/products/{product_id}` must not let operators set `status="inactive"`.
- Revoked account sessions: disabling or resetting a user must invalidate dashboard cookies and extension bearer sessions already issued to that user.
- Forced password change: a first-login user must be able to change their password and log out, while other dashboard pages and business APIs remain unavailable.
- Service bearer tokens: existing path-scoped integration tokens must retain their intended service access and must not be interpreted as an administrator.

---

### Task 1: Add database roles and central auth capabilities

**Files:**
- Modify: `app/db/models.py`
- Modify: `app/core/auth.py`
- Create: `alembic/versions/20261028_0017_user_roles.py`

**Interfaces:**
- `User.role: str` accepts only `admin` or `operator`.
- `User.must_change_password: bool` defaults to `False`.
- `require_admin(request: Request, db: Session = Depends(get_db)) -> User` returns the authenticated admin or raises `401`/`403`.
- The configured `AUTH_ADMIN_USERNAME` remains the bootstrap source for the sole administrator.

- [x] **Step 1: Add role and password-change fields to `User`**

Add a database check constraint for `role IN ('admin', 'operator')`; default existing and new non-admin records to `operator`.

- [x] **Step 2: Add the additive migration**

Create revision `20261028_0017`, down revision `20261021_0016`; add `role` and `must_change_password` with safe server defaults. Do not modify any monitoring, queue, session, or history data.

- [x] **Step 3: Add role-aware auth helpers**

Implement `require_admin` and a helper that allows the configured admin username to remain the administrator across startup. Update bootstrap so it creates the initial admin when the users table is empty and idempotently preserves the configured admin role when the row already exists.

- [x] **Step 4: Add the forced-password-change gate to authentication dependencies**

Keep `/api/auth/me`, `/api/auth/logout`, and `/api/auth/change-password` available while `must_change_password` is true. Redirect protected pages to `/change-password` and reject other business API requests until the password is changed. Keep existing service-token path restrictions unchanged.

- [x] **Step 5: Review migration defaults and admin bootstrap paths**

Inspect the migration and auth diff to confirm an existing configured admin remains admin, no non-admin user is promoted, and existing sessions/data are not deleted.

### Task 2: Add admin-managed account APIs and password lifecycle

**Files:**
- Modify: `app/api/auth.py`
- Create: `app/services/user_accounts.py`
- Modify: `app/db/models.py` (session revocation relationship/query use only)

**Interfaces:**
- `GET /api/auth/users` returns user ID, username, role, active state, and created time; never returns a password hash.
- `POST /api/auth/users` creates an `operator` and returns its one-time initial password.
- `POST /api/auth/users/{user_id}/reset-password` rotates the password, sets `must_change_password`, revokes all sessions, and returns the one-time password.
- `POST /api/auth/users/{user_id}/disable` marks the account inactive and revokes all sessions.
- `POST /api/auth/change-password` changes the current account password and clears `must_change_password`.
- All account management endpoints require `require_admin`; self-password change requires only the authenticated user.

- [x] **Step 1: Implement account service operations**

In `app/services/user_accounts.py`, implement operator creation, temporary password generation, password reset, account disable, and current-user password change. Use `hash_password`, `verify_password`, and session revocation from `app/core/auth.py`; prevent disabling or resetting the configured admin.

- [x] **Step 2: Define request and response schemas**

Extend the auth API models so account responses expose only public fields and the temporary password is present only in successful create/reset responses.

- [x] **Step 3: Implement administrator-only account routes**

Add list, create, reset, and disable routes in `app/api/auth.py`; reject duplicate usernames, inactive targets, and any attempt to target the configured admin.

- [x] **Step 4: Implement self-service password change**

Require the current password for ordinary changes; require a valid new password and clear the forced-change flag only after the password hash is committed.

- [x] **Step 5: Review returned user fields and session revocation behavior**

Inspect responses and service queries to confirm password hashes are never serialized and all existing sessions for disabled/reset users are revoked.

### Task 2 (complete): Add admin-managed account APIs and password lifecycle

### Task 3: Enforce operator restrictions on destructive APIs

**Files:**
- Modify: `app/api/router.py`
- Modify: `app/core/auth.py`

**Interfaces:**
- All restrictions use the `require_admin` dependency defined in Task 1.
- Existing authenticated worker and collection endpoints remain available to operators unless their operation removes data or cancels the remaining run.

- [x] **Step 1: Protect store and product deactivation**

Require admin for `POST /api/stores/{store_id}/deactivate` and `POST /api/products/{product_id}/deactivate`. In `PATCH /api/stores/{store_id}` and `PATCH /api/products/{product_id}`, reject an operator's attempt to set status to `inactive` while allowing other edits.

- [x] **Step 2: Protect product-pool clearing and replacement**

Require admin for `POST /api/products/clear-active` and all user-facing pool-reset mutations, including create, start, cancel, and retry. Keep worker claim, heartbeat, challenge resume, and result-reporting routes available to the authenticated collector.

- [x] **Step 3: Protect permanent Dianxiaomi record deletion**

Use role-based admin authorization for single and bulk deletion of failed Dianxiaomi handoffs. Preserve the existing failure-only deletion constraint.

- [x] **Step 4: Protect cancellation of the remaining browser collection run**

Require admin for `POST /api/browser-collection/runs/{run_id}/cancel`. Keep single Dianxiaomi handoff cancellation available when it marks only that record canceled and retains it.

- [x] **Step 5: Review the API route matrix**

Discovery behavior was also reviewed: operator-triggered store discovery still adds and updates observed products, but it does not implicitly deactivate active products missing from that scan. All user-facing pool-reset mutations require an administrator; worker claim, challenge resume, and result reporting retain their scoped collector flow.

Use a route inventory to confirm every removal/clear/whole-run-cancel path has an admin guard and ordinary collection, identity-review, and queue pause/resume routes remain available to operators.

### Task 4: Add account pages and role-aware dashboard controls

**Files:**
- Modify: `app/dashboard/router.py`
- Modify: `app/core/auth.py`
- Modify: `app/api/auth.py`
- Modify: `app/templates/base.html`
- Modify: `app/templates/dashboard.html`
- Modify: `app/templates/login.html`
- Modify: `app/templates/store_detail.html`
- Modify: `app/templates/product_detail.html`
- Create: `app/templates/users.html`
- Create: `app/templates/change_password.html`
- Modify: `app/static/dashboard.js`
- Modify: `app/static/dashboard.css`

**Interfaces:**
- Page context exposes the authenticated user's role and forced-password-change state without trusting query parameters or browser-submitted role values.
- Admin account navigation and restricted action controls render only for `admin`.
- Dynamic Dianxiaomi controls use the server's role-derived `can_delete_failed` capability.

- [x] **Step 1: Expose authenticated user context to Jinja pages**

Update page-auth handling to make the authenticated user available to templates. Add an admin-only `/users` page route and the `/change-password` page route.

- [x] **Step 2: Add account management UI**

Create `users.html` with a user list and create/disable/reset forms. Display the initial/reset password once with an explicit copy-and-deliver instruction, then clear it from the page state when dismissed or navigated away.

- [x] **Step 3: Add first-login and self-service password UI**

Create `change_password.html`; route successful login to it when `must_change_password` is set and redirect to the dashboard after a successful password change.

- [x] **Step 4: Hide restricted dashboard controls by role**

Hide store/product monitoring removal, product-pool clearing, pool replacement, failed-handoff deletion, remaining-run cancellation, and account management controls from operators. Keep normal collection, edit, review, pause, and resume controls visible.

- [x] **Step 5: Update page styling and asset cache version**

Style the account and password pages using the existing dashboard visual system and bump the dashboard asset query version so clients load the role-aware UI.

- [x] **Step 6: Review page/API capability parity**

Compare each restricted control with the server-side route guard and confirm every UI-hidden operation has a backend restriction, while permitted collection actions remain present.

### Task 5: Final implementation review and handoff

**Files:**
- Review: all files changed by Tasks 1–4
- Modify: `docs/superpowers/specs/2026-10-28-monitor-dashboard-user-roles-design.md` only if implementation decisions require a user-approved spec correction

- [x] **Step 1: Review the implementation against the role matrix**

Check admin/operator behavior for shared reads, add/edit/collect access, all restricted removal paths, and account management boundaries.

- [x] **Step 2: Review compatibility boundaries**

Confirm existing admin bootstrap, integration tokens, extension session flow, and existing collection queue/data are not broadened or reset.

- [x] **Step 3: Report source and operational status separately**

Report modified files, migration presence, and any requested verification. Do not claim NAS deployment or collector acceptance; that requires a separate user request and deployment authorization.
