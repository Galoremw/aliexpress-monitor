# IXSPY Data Source Report

## Conclusion

IXSPY appears to maintain a server-side historical dataset populated by periodic
or broad collection. The user does not need to open a product every day for the
one-year trend chart to exist. IXSPY's public product description says that
product data is updated daily and that trend data is retained for a year, but no
documented public historical-sales API was found.

This is an inference from the visible chart and official product description,
not a claim about IXSPY's private implementation.

## Observed Data

- The visible chart exposes `总量` and `增量` modes.
- A visible tooltip such as `2026-09-06 / 近一年销量 9,159` is a cumulative
  total, not that day's orders.
- `增量` is the preferred daily-sales value when it is visibly available.
- When only cumulative totals are visible, a daily value is derived only from
  consecutive, non-decreasing dates.
- Hidden canvas pixels, private requests, credentials, cookies, and tokens are
  not used.

## Monitor Import Contract

The Chrome collector sends explicit points:

```json
{
  "date": "2026-09-22",
  "value": 14,
  "value_type": "daily_increment"
}
```

or:

```json
{
  "date": "2026-09-22",
  "value": 125,
  "value_type": "cumulative_total"
}
```

The original points are stored in `ProductSnapshot.raw_data.historical_sales`.
The derived daily metric is stored separately and never replaces the Snapshot.

## Initial and Daily Flow

1. On first collection, the extension reads the visible IXSPY history and can
   import 19, 20, and 21 directly when `增量` points are visible.
2. If only cumulative points are visible, the backend computes a point only
   when the previous calendar date is available and the value did not decrease.
3. On a later run, the extension may submit only the new 22 cumulative point.
   The backend reads the 21 point from earlier raw Snapshots and derives the 22
   daily increment.
4. A missing date, a decreased cumulative value, or a parse failure produces
   no fabricated metric.
5. Repeated submission is idempotent for the same product/date metric while
   every collection remains a new raw Snapshot.

## Verification Status

Local unit tests cover direct daily points, cumulative consecutive points, and
the later single-point baseline flow. The latest browser collector is version
`0.4.1`.

The public Render backend still exposes the old request schema without
`historical_sales`; therefore the cloud end-to-end import is not yet verified.
Render must complete a Blueprint sync or deploy the latest commit before a real
cloud Snapshot can prove the loop.

## Security Boundary

The implementation uses only normal, user-visible page data and normal manual
verification. It does not bypass CAPTCHA or anti-bot controls, read cookies or
tokens, reverse-engineer protected endpoints, or claim that estimated sales are
real backend orders.
