# Store-only daily IXSPY collection

## Goal

At 02:00 Asia/Shanghai, collect the latest seven IXSPY daily increments for each
active monitored product from its store's Orders-sorted product listing pages.
The scheduled worker must never open an individual product detail page. Initial
top-20 selection by seven-day increment remains a separate manual workflow.

## Queue and targeting

- A daily run contains one `STORE` item per active store with monitored products.
  It creates no automatic `PRODUCT` items. Existing explicit `:single` product
  tests remain available but are not part of the scheduled path.
- The claim response gives the worker a fixed list of that store's active
  AliExpress product IDs. Store-only collection does not discover, add,
  deactivate, or reorder monitored products.
- Pending automatic product items left by older runs are skipped only when
  that run is actively advanced. Paused or cancelled runs are not rewritten
  merely by deploying code.
- Clicking one store's Collect button after the day's run was cancelled
  requeues only that store item. Other cancelled items stay skipped; this
  does not resume the entire daily batch.

## Browser collection

- Start at the canonical Orders-sorted `all-items` URL. On each page, scan
  product IDs and read IXSPY charts only for IDs in the fixed target list.
  Match every chart back to its card and product ID before accepting values.
- Follow observable next-page links for the same store and Orders sort until
  all targets are found, there is no next page, or a bounded page/time limit
  is reached. Never navigate to an item detail URL as a fallback. A missing
  or unresolvable next-page control produces a partial result, not guessed
  product data.
- Keep challenge and IXSPY-login handling human-controlled. Pause the task
  on verification; do not bypass it or silently continue with empty data.
- Return visited store page URLs, target IDs, found IDs, expected dates,
  missing IDs/dates, source and diagnostics. The collector may retry an
  incomplete chart on the same listing page, but not visit product pages.

## Persistence and missing values

- For queued store results, validate the store identity, target IDs and
  `daily_increment` dates. Reject an unexpected product ID or a cumulative-
  like series before writes. A legacy extension payload that lacks the new
  store-only contract must fail closed; it cannot overwrite the monitoring
  pool.
- Import observed daily values, including a visible zero, as estimable IXSPY
  daily increments. For an unobserved expected date, store a provisional zero
  with `is_estimable=false` and a distinct pending reason; never replace a
  previously confirmed IXSPY value with a provisional zero. Retry missing
  dates on the next store-page run.
- A store item is `SUCCEEDED` only when all targeted products have the seven
  expected dates. Otherwise it is `PARTIAL` with missing-ID/date diagnostics.
  Store totals include only confirmed values and retain an unavailable count.
- A store with no active monitored products is not auto-populated by a daily
  task. Its top-20 intake still requires the manual seven-day ranking flow.

## Compatibility, UI, and verification

- Change the dashboard's daily-queue wording to store-page collection and
  show partial/pending coverage without calling provisional zero observed.
- Preserve historic snapshots and queue records. Do not revive the currently
  cancelled run, restart containers, or reload the Chrome extension as part
  of the source change.
- Tests cover queue generation/advancement, cancelled-run single-store
  requeue, ID filtering, pagination, challenge stop, no item URL navigation,
  backend rejection of legacy/mismatched payloads, provisional zeros, and
  preservation of the selected top-20 pool. Run focused and full suites.
- Deployment is a separate authorized step: backup and deploy backend,
  reload extension, then have the user launch a visible store-page test.
  Verify the resulting 20 IDs and seven dates in the database. Unfocused
  operation and the overnight schedule require their own live acceptance
  checks and cannot be inferred from unit tests.
