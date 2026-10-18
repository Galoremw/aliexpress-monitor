# Store Identity Human Review and URL Alias Design

**Status:** 用户已确认；源码与自动化测试已完成，生产部署另行授权
**Date:** 2026-10-18

## Goal

Allow a human to resolve an ambiguous AliExpress store identity once, continue that verified collection, and retain a durable, store-scoped URL identity record that can be checked by later collection tasks on either computer. Do not globally weaken store identity validation.

## Confirmed Requirements

- Show two actions on a queued store task only when store identity is unverified or mismatched: `确定无误` (green background, black text) and `错误` (red background, black text).
- `确定无误` means the user has inspected the current page and confirms it belongs to the task's monitored store. It may override the identity rule for this exact current task and records the observed URL identity for future comparisons.
- `错误` rejects only the current task, stores no URL alias, and leaves the queue paused. This behavior was explicitly selected by the user.
- Later collection may use an approved URL identity only when it matches the same monitored store and the saved evidence is consistent. A new or conflicting identity returns to human review.
- The backend is authoritative so the approved record is shared by the primary and second computer.
- Do not provide a way to bypass AliExpress login, CAPTCHA, or other platform verification. Identity review and platform verification are separate states.
- Do not change canonical AliExpress store IDs or automatically rewrite the monitored store URL.
- Do not restart services, apply migrations to the live database, resume the live queue, or reload either extension as part of this design review.

## Current Behavior and Gap

The current queue represents identity review through the same `NEEDS_VERIFICATION` path as platform challenges. The extension sends the identity review through the challenge handler; the backend records it as `platform_challenge`. Although the challenge request schema has a `page_url` field, the browser-collection challenge route currently passes only the error message to the service, and the extension does not submit the observed page URL for this identity case. Therefore the dashboard cannot reliably show identity-only actions or persist the exact URL that the user reviewed.

The extension and backend already validate page-bound store identity and reject conflicting evidence. The change should add an explicit human decision and a durable alias record without removing those checks for unapproved URLs.

## Approaches Considered

1. **Extension-local approval:** Quick to add, but it would not be shared between computers, could be lost with the browser profile, and would be difficult to audit. Rejected.
2. **Store URL history embedded in a collection snapshot:** Avoids a new table, but mixes identity configuration with immutable collection results and makes uniqueness, conflict detection, and transactional approval less clear. Rejected.
3. **Backend store-scoped URL identity record (selected):** Store each human-approved route identity in a dedicated table, bind it to one monitored store, and expose it through the existing queued-task API. This is durable across machines and supports uniqueness checks and audit evidence.

## Proposed Workflow

1. When a store page cannot be verified, the extension submits the observed page URL, route ID, page title, any visible Store ID values, and available IXSPY store-overview name as identity evidence. The backend records a distinct identity-review reason and pauses dispatch. Actual platform challenges continue through the existing challenge reason.
2. The dashboard renders the two requested actions only on an identity-review task. The task card continues to show the expected store, observed URL, and diagnostic evidence.
3. **Confirm:** The backend transaction verifies that the task is still the current `NEEDS_VERIFICATION` store task, that its monitored store is unchanged, and that the submitted URL/evidence matches what the extension reported. It creates or reuses the store-scoped URL identity record, records the decision time and source task, and marks the exact task approved. The selected worker then resumes that task; normal queue processing continues. Any later unknown identity pauses again.
4. **Reject:** The backend marks the current item failed with a human-rejection reason, keeps the run paused, and creates no URL identity record. Pending tasks are not dispatched until the user resumes the queue.
5. If a future task uses a URL identity already approved for that same store and the page evidence is consistent with the saved record, the extension may continue automatically. An unknown route ID, a route assigned to another store, changed/conflicting explicit Store ID, or inconsistent available store name returns the task to human review.

## URL Identity and Persistence

- Store the complete observed URL for audit, but compare a normalized identity consisting of AliExpress host plus `/store/{route_store_id}/pages/all-items.html`. Query parameters such as page number, sort, tracking, or `spm` do not define store identity.
- Bind each route ID to exactly one monitored store. A route ID already bound to a different store cannot be silently reassigned; it requires review and an explicit data correction path.
- Persist the monitored `store_id`, observed `route_store_id`, normalized page identity, first reviewed URL, available page title/Store ID/IXSPY name evidence, source queue item, and confirmation timestamp. Keep the canonical `Store.aliexpress_store_id` and `Store.url` unchanged.
- Preserve the evidence used for the human decision. Missing fields remain missing; the system must not fabricate a Store ID or store name.
- A duplicate confirmation for the same store and route ID is idempotent. A conflicting confirmation cannot overwrite the prior record silently.

## API, Worker, and UI Changes

- Add explicit confirm and reject actions for the current identity-review item. Both actions are state-checked and idempotent; stale task/run state returns a conflict instead of changing another task.
- Extend identity-review reporting to carry the observed URL and evidence to the backend. Do not treat ordinary CAPTCHA/login challenges as store-identity review.
- Include approved route identities in the queued store task data so any compatible extension worker can apply the same backend rules. The extension must still check the current host/path and compare available identity evidence before collection.
- Add the two actions to the existing queue item card. Use green and red backgrounds respectively, black text as requested, and disabled/loading states while an action is being saved. Hide them for platform challenge items and all other task states.
- The confirm action is an explicit human authorization to resume normal queue dispatch. The reject action leaves the queue paused and does not discard unrelated pending tasks.

## Failure and Safety Rules

- Unknown or conflicting identity evidence never creates an alias automatically.
- Human approval applies to one monitored store and one normalized URL identity; it is not a global ignore flag.
- A route identity already assigned to another monitored store cannot pass automatically, even if the display name is similar.
- If a later page exposes a conflicting explicit Store ID or inconsistent recorded store evidence, pause for review rather than trusting the URL alias alone.
- A platform challenge remains paused until the user completes it in the browser; neither identity button can bypass it.
- If persistence fails, do not resume the task. Return an actionable error and keep the queue paused.

## Testing and Acceptance

### Backend

- Confirm accepts only the current identity-review task and saves the normalized URL identity plus evidence atomically.
- Reject marks only the current item, creates no alias, and leaves the run paused.
- Duplicate confirmation is idempotent; stale tasks, changed store associations, invalid URLs, and route-ID collisions are rejected.
- A platform challenge cannot use the identity-review endpoints.
- Future task serialization includes only aliases for its own store; an alias from another store never passes validation.
- Matching approved evidence is accepted; unknown routes and changed/conflicting evidence require review.

### Extension

- Identity review reports the actual tab URL and observed evidence; CAPTCHA/login continues to use the challenge flow.
- A human-approved current task resumes only for its bound store and page. Subsequent known aliases are checked against the backend record on either worker; unknown or mismatched aliases pause before reading product charts.
- Stale approval, tab URL change, or task/store change does not bypass identity checks.

### Dashboard

- The green `确定无误` and red `错误` controls appear only on identity-review items, have black text, and display pending/error states correctly.
- Confirm refreshes the task/run state; reject shows the failed task and keeps the queue paused.

### Live acceptance (separate from source tests)

After implementation and separately authorized deployment, test the currently reviewed store on one computer: confirm its observed URL, verify that the same task continues, then verify the stored route record is available to the second computer. Test an unknown route, a conflicting explicit Store ID, and a true CAPTCHA separately; each must remain blocked for the appropriate human action. Source tests do not prove production deployment or successful IXSPY collection.

## Current Environment Snapshot

The user-provided 2026-10-18 screenshot shows a paused daily queue with 16 total tasks: 1 successful, 4 partial, 10 pending, and 1 awaiting verification. No queue, database, service, or extension state is changed by this design document.

## Out of Scope

- Automatically rewriting a monitored store's canonical ID or URL.
- Inferring identity from store name alone or from URL route ID alone.
- Global identity-check bypasses, automated CAPTCHA/login handling, or stealth behavior.
- Deleting collection history or changing monitored products.
- Automatically deploying the change, running a database migration, restarting backend services, or continuing the current live queue.
