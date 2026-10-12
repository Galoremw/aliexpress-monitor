# Store-Page Pool Fill Design

**Status:** Awaiting user review  
**Date:** 2026-10-12

## Purpose

Make store-level IXSPY collection fill a monitored store's product pool page by page. A first Orders page must be useful immediately: eligible products are enrolled without waiting for 20 complete candidates. The scheduled store task then continues from a durable page cursor, updates the existing monitored products, and fills only vacant pool slots from later Orders pages.

## Confirmed Requirements

- A store may have fewer than 20 active monitored products while eligible products have not yet been found. The target is 20; a store with fewer eligible products remains below 20.
- Enroll eligible products from the current page immediately, even when that page contributes fewer than 20 products or none.
- Keep every already-enrolled product. Later, higher-scoring candidates do not replace earlier products.
- On each page, rank eligible new candidates by the sum of the latest seven complete IXSPY `daily_increment` values, descending; use Orders-page rank as the tie-breaker.
- The daily store task updates all current monitored products and scans at most one additional candidate-discovery page per store per run. It does not open product detail pages.
- Only a complete seven-day history visibly read as daily increments qualifies a new product. A visible zero is valid; a missing date is not inferred as zero for enrollment.
- A structurally valid page with no eligible candidates is recorded and advances the cursor. A challenge, wrong store, unrecognized Orders page, navigation failure, or page-readiness failure leaves the cursor unchanged.
- A candidate with unavailable/incomplete chart data is skipped with diagnostics and can be encountered again after the page cursor wraps.
- Product IDs are deduplicated. Previously inactive products are not automatically reactivated. Existing products are never deleted, deactivated, or replaced by this workflow.
- Preserve the existing daily schedule and current missing-data handling for already-monitored products.
- Code and automated tests may be changed after the written design and implementation plan are approved. Do not restart services, mutate the live queue/database, or reload Chrome without separate user authorization.

## Current Problem

The browser extension stages candidate pages locally and the ranked enrollment route expects a complete selected set before it persists products. When a page has fewer than 20 complete histories and more Orders pages remain, the user must continue manually; no eligible product is written yet. Daily store tasks are currently built from stores joined to active products, and the extension's daily page walk is aimed at finding existing monitored IDs rather than discovering new candidates. Consequently, a new or underfilled store can have no durable continuation path.

The current workspace also contains unrelated uncommitted source and test changes. Implementation must inspect and preserve those changes; this design does not authorize reverting or absorbing them into a commit.

## Approaches Considered

1. **Extension-local cursor:** Small server change, but progress belongs to one Chrome profile and can be lost or diverge across the two collection computers.
2. **Cursor embedded in StoreSnapshot JSON:** Avoids a new table, but mixes mutable continuation state with immutable collection evidence and makes concurrent advancement and recovery harder to reason about.
3. **Dedicated backend state per store (recommended):** Keep one authoritative cursor on the backend and retain each page's evidence in StoreSnapshot. This works across collection computers and supports deterministic retry and audit.

The approved design uses approach 3.

## Architecture

### Per-store page state

Add a backend-owned discovery-state row keyed by `store_id`. It records the canonical first Orders URL, the next page URL to scan, the last successfully scanned URL/time, and the last outcome/error. Add it through an Alembic migration. StoreSnapshot remains the audit record for each page attempt, including visible candidate IDs, page URL, candidate chart outcomes, and cursor before/after.

The backend is authoritative. Extension storage may keep in-flight page data while a single page is being read, but it is not the durable cross-computer cursor.

### Initial/manual enrollment

The popup reads the current store Orders page and IXSPY card charts, validates store identity and Orders sorting, then submits that page's complete eligible histories immediately. It does not buffer earlier pages while waiting for 20 candidates. The backend appends up to the available slots and records the page snapshot even when zero candidates qualify. If a next Orders URL is known, it becomes the next cursor; a repeated submission is idempotent by store/product ID and must not move the cursor backwards.

If the pool is already full, the page may still be recorded, but no new active product is added. A page with unreadable candidate charts records those outcomes; it does not turn missing dates into zeroes.

### Daily store task

Keep one store-level queue item per active store, including stores with zero monitored products. The worker:

1. Reads the current active monitored product IDs from the backend.
2. Runs the existing store-page collection needed to update those IDs' daily metrics, with no detail-page navigation.
3. If the active pool has fewer than 20 products, reads the cursor page (reusing it if already visited during step 2 when possible), parses its cards, and reads visible IXSPY histories for untracked candidates.
4. Ranks only eligible candidates on that page, then appends at most the number of open slots.
5. Submits existing-product metrics and candidate-discovery outcomes so each phase has an explicit result.

The candidate-discovery portion is bounded to one cursor page per store task. Reuse a page already visited for monitored-ID collection when possible; otherwise make at most one additional page visit. Preserve the existing bound on monitored-ID page walking and account for the one discovery-page visit in the overall visited-URL validation and task timeout. If the task deadline is reached before the candidate page is fully scanned, record partial diagnostics and keep the cursor on that page for the next run. When the last Orders page is successfully scanned, set the next cursor to the first Orders page for a later run. A full pool stops candidate scanning while ordinary daily updates continue; if a slot is later opened manually, discovery resumes from the preserved cursor.

### Candidate and pool rules

- Eligibility requires exactly the expected seven distinct dates and direct visible IXSPY `daily_increment` values for all seven dates.
- A numeric zero is preserved as a real observation. Missing dates, cumulative series, inferred deltas, and unavailable charts do not qualify.
- Rank by seven-day sum descending, then Orders-page rank ascending. Only candidates from the page being processed compete for that run's open slots; there is no global re-ranking across unvisited pages.
- Cap automatic additions at `max(0, 20 - active_product_count)`. If legacy/manual state already exceeds 20, preserve it and do not add more until the active count falls below 20.
- Existing active products are append-only under this workflow. Existing inactive IDs stay inactive. Unique `(store_id, aliexpress_product_id)` identity prevents duplicate rows.

## API and Persistence Changes

- Extend the current browser-extension store-selection submission so partial or empty page results can be recorded and eligible products appended without deactivating products absent from that page.
- Extend the daily monitored-store submission to include candidate page URL, candidate records, next-page URL, and page-read diagnostics alongside current monitored-product results.
- Validate platform store ID, normalized Orders URL, current cursor (or valid first-page initialization), candidate IDs/URLs, seven-day date window, direct increment source, and vacancy limit server-side. The client cannot choose a cursor arbitrarily or submit cumulative-series candidates.
- Persist product inserts, the candidate's seven-day metrics, candidate-page StoreSnapshot evidence, and successful cursor advancement in one transaction. A failed validation or persistence must not partially insert candidates or advance the cursor.
- Keep existing monitored-product daily metric persistence independent from candidate-page enrollment. If candidate reading fails after the existing-product phase succeeds, retain those daily metrics and finish the queue item as partial with candidate diagnostics.
- Keep existing monitored-product missing-data semantics separate from candidate eligibility. A missing candidate chart is diagnostic/skip, not a zero-valued enrollment history.

## Failure Handling and Recovery

- **Challenge or IXSPY login required:** Pause for human handling; preserve cursor and the same queue item.
- **Wrong store, wrong sort, malformed page, or navigation timeout:** Fail/partial the task with page diagnostics; do not advance cursor.
- **One candidate chart unavailable/incomplete:** Skip that candidate, record the reason, and advance after the page structure itself has been successfully scanned. It is retried after wraparound.
- **Backend timeout after browser read:** Retry may resubmit the same page; product IDs and metric dates are idempotent. Do not regress the cursor on duplicate/stale submissions; the only intentional cycle back is a positively confirmed last-page-to-first-page wrap.
- **No next-page link:** Treat as the last page only when pagination controls positively establish the end. Otherwise retain the cursor and report an unrecognized pagination state.
- **Pool full:** Do not scan candidate pages or change the cursor; continue daily updates for existing monitored products.

## Testing

### Extension tests

- Submits a page's eligible products immediately with fewer than 20 candidates and records a page with zero eligible candidates.
- Reads one cursor page per scheduled store task, reads cards sequentially, and never opens a candidate product detail page.
- Includes exact candidate and pagination diagnostics; missing charts do not become zeroes.
- Challenge, wrong-store, wrong-sort, malformed pagination, and timeout do not claim successful cursor advancement.
- Handles the last-page wrap and avoids duplicate IDs or moving a cursor backwards on repeated/stale submissions.

### Backend tests

- Schedules active stores with zero, partial, and full pools.
- Appends eligible products to open slots; enforces the 20-product automatic cap; preserves existing active/inactive states and never deactivates absent products.
- Rejects incomplete/cumulative candidate histories and accepts visible zeroes only when all seven dates are present.
- Persists page evidence and cursor atomically; retries are idempotent; failures preserve the previous cursor.
- Advances on a structurally valid zero-eligible page and wraps after the last page.

### Live acceptance (separate from source tests)

After the approved implementation is deployed and the extension reloaded by the user, test one underfilled store: manually read page 1 and confirm immediate partial enrollment; run one store task and confirm page 2 is read and only vacant slots are filled; verify existing product IDs remain unchanged, no detail pages open, and page/candidate evidence appears in the monitoring console. Test verification handling separately. Source tests alone do not prove live IXSPY collection or unattended 2:00 AM reliability.

## Explicit Trade-off

This design intentionally favors preserving products and making incremental progress over a whole-store global Top 20. A later page may contain a higher-scoring product, but it will only fill an open slot; it will not evict a product already in the pool. Completing a pool can take multiple daily runs, depending on page count, chart availability, and verification.

## Out of Scope

- Replacing existing products with higher-ranked later candidates.
- Opening product detail pages for daily store collection.
- Bypassing AliExpress/IXSPY verification, login, or anti-bot controls.
- Reconfiguring the 2:00 AM schedule, restarting containers, reloading Chrome, or changing the live queue/database as part of implementation.
- Claiming background/unattended reliability without separate live and overnight acceptance.
