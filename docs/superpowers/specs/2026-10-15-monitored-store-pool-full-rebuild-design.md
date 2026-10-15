# Monitored Store Pool Full Rebuild Design

**Status:** Draft for user review
**Date:** 2026-10-15

## Goal

Provide a one-time, explicit rebuild of monitored-product membership for every active AliExpress store. For each store, rank eligible products from the first 60 Orders-sorted candidates using complete, visible IXSPY daily increments for the latest seven complete dates, then select up to 20. Preserve product records and all historical observations.

## Confirmed Scope

- Process the 17 stores whose store status is `active` at request time.
- Leave all three `inactive` stores, their products, and their histories untouched. The current data includes one inactive store that still has 20 active product records; that anomalous combination is deliberately outside this operation.
- Do not change store names, canonical store IDs, or store URLs.
- Do not reinterpret a URL-ID mismatch as an alias. Existing strict store-identity validation remains in force until separately approved.
- Use the confirmed candidate rule: scan up to the first 60 products on Orders-sorted store pages; rank products with complete seven-day IXSPY `daily_increment` data by the sum of those seven values, descending, then Orders rank ascending, then product ID ascending. Select at most 20 eligible products per store. This is not a claim of the store's global top 20.
- Freeze the reset target date and the seven dates immediately before it when the batch is created; every store in that batch uses the same date window even if collection crosses midnight.
- Products with confirmed absent/incomplete seven-day IXSPY history are excluded and diagnosed. If a fully and validly scanned candidate range yields fewer than 20 eligible products, enroll that smaller set; do not fabricate values or wait for exactly 20.

## Proposed Workflow

1. Create a distinct `POOL_RESET` batch for the current active stores. Keep it separate from the existing daily run so the completed/partial daily run and its task diagnostics remain intact.
2. The reset batch starts pending with dispatch disabled. The dashboard shows its per-store state and requires the user to select an online collection computer and click the start control. It never opens Chrome tabs or starts collection merely because the reset was requested.
3. The selected extension worker claims one bounded store-page task at a time. It opens that store's Orders page, reads candidate charts on that page, and follows Orders pagination only as needed to cover at most 60 candidates. It does not open product-detail pages. When another page is needed, the backend queues a continuation for the same store rather than requiring one long browser claim to read all 60 charts.
4. Persist staged candidate observations and page progress on the backend, keyed by reset batch and store. Do not rely only on extension-local storage, so a browser restart or worker handoff cannot silently lose or mix a store's staged results.
5. Once the candidate range is exhausted or 60 candidates have been fully classified, rank eligible candidates and replace that store's active pool in one database transaction.

## Per-Store Replacement and Data Safety

- The operation is atomic per store, not across all 17 stores. The dashboard must show which stores are pending, collecting, replaced, or retained after failure.
- A store's old active membership remains unchanged until that store has a complete, identity-validated candidate scan. CAPTCHA, URL/store-ID mismatch, page failure, timeout, or ambiguous IXSPY chart-read failure leaves that store's old pool untouched and records a diagnostic.
- If a store is deactivated after batch creation but before its replacement transaction, skip that store and retain its pool unchanged.
- On successful replacement, mark old active products not selected as inactive; do not delete them. Reactivate selected existing product records by ID, insert genuinely new products, and update current discovery rank/title/URL fields as appropriate.
- Preserve snapshots, daily metrics, reviews, collection attempts, and all other historical rows for both selected and deselected products. Physical deletion is forbidden in this workflow because product foreign keys cascade to historical data.
- Apply each store's membership changes and the reset task's successful terminal state in the same transaction. Repeated submission of the same claim/result must be idempotent.
- If a valid complete scan finds no eligible candidate, the successful replacement may leave that store with an empty active pool, with the rejected candidates and reasons visible in diagnostics.
- Canceling an unfinished batch does not roll back stores already replaced; stores not yet successfully replaced retain their prior pools.

## Queue, Worker, and Identity Rules

- Reset work must not be represented by mutating or clearing today's existing daily-run rows. The daily run remains auditable and unchanged.
- Only the selected online worker can claim reset tasks. At most one store task is active for this batch at a time; claim token, worker ID, reset batch ID, store ID, and target URL are validated on every write.
- Reset dispatch and daily collection dispatch are mutually exclusive. Starting a reset is rejected while a daily task is actively claimed; while reset work is running, daily tasks cannot be claimed or started.
- A reset task is bound to its claimed store and expected Orders URL. Before scanning each page and before submitting staged results, verify the tab's store identity. A mismatch fails that store without changing its pool.
- The dashboard must not label a batch as collected until the backend commits the per-store replacement. Queue creation, task claim, page opening, and successful pool replacement are distinct states.

## Failure and Retry Behavior

- A transient worker loss may return the current store task to pending within the existing bounded retry policy; persisted staging is retained only when its claim and page identity remain valid.
- Identity mismatch, repeated timeout, invalid cursor, incomplete page enumeration, or stale claim is diagnostic and cannot advance the reset candidate cursor or mutate membership.
- Retrying a failed store starts from its persisted safe checkpoint or the first uncommitted page. It cannot reuse candidate ranks or chart values from another store, target date, or reset batch.
- Already committed stores remain complete if a later store fails. The batch ends as partial when at least one store succeeds and another fails; it ends failed when none succeeds.

## UI and Compatibility

- Add a clearly separate reset progress section/action in the dashboard. It must summarize scope (17 active stores), candidate boundary (first 60), ranking basis (seven complete IXSPY daily increments), and the fact that historical observations are retained.
- The action requires an explicit confirmation before creating a batch because it replaces active pool membership. Confirmation does not start the browser worker.
- The daily collection path and its existing fill-empty-slots/no-eviction policy remain unchanged.
- Existing extension versions that do not understand reset tasks must not claim them. The backend should expose a minimum worker capability/version for this task type and show a clear blocked state instead of dispatching incompatible work.
- Do not automatically restart services, reload extensions, change worker selection, resume/cancel the daily queue, or distribute an extension archive as part of this workflow.

## Acceptance Criteria

1. The reset batch includes exactly the active stores captured at creation and one frozen seven-date window immediately preceding its target date; inactive stores are untouched.
2. The existing daily run's status, items, and errors remain unchanged.
3. Each store scans no more than 60 Orders candidates and selects no more than 20 products using the confirmed seven-day ranking and tie-break order.
4. Missing chart history is excluded with a reason; incomplete or mismatched page evidence cannot alter that store's active pool.
5. Successful replacement deactivates nonselected products and reuses/reactivates matching rows without deleting historical data.
6. A simulated failure in one store leaves its pool unchanged while another successful store can commit independently.
7. Only the selected online worker can claim reset tasks; duplicate/stale claim submissions cannot apply results twice or cross-apply another store's candidates.
8. Creating a reset batch leaves it pending and does not open pages; explicit worker selection and start are required.
9. Extension tests cover pagination, store-task binding, chart classification, and no product-detail navigation; backend tests cover ranking, transactional replacement, preservation, and failure isolation.

## Current Environment Snapshot

Read-only inspection on 2026-10-15 found 20 stores total: 17 active and 3 inactive. Sixteen active stores currently have 20 active products and `辣子鸡老店1` has 16. The inactive stores have 0, 20, and 0 active products respectively. Today's daily run is `PARTIAL`/`COMPLETED` with dispatch disabled. No pool or queue changes were made while preparing this draft.
