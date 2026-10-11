# IXSPY Data Source Contract

## Source

The extension reads values only from IXSPY charts visible to the signed-in user. It first opens each store-listing card's trend panel in sequence and verifies the visible chart's product ID against the card. Product-detail charts remain a fallback for incomplete store-page reads. The collector selects daily-increment mode and reads the latest seven dated tooltip values. It does not call private endpoints, inspect hidden application state, or collect account credentials.

## Accepted Values

Each imported point has this shape:

```json
{
  "date": "2026-09-22",
  "value": 14,
  "value_type": "daily_increment"
}
```

The tooltip must expose both a date and a signed integer. A visible zero or negative correction is preserved as shown. An unread date stays missing; the collector never fills it from cumulative totals or estimates it from adjacent snapshots. A seven-day result is complete only when all seven expected dates were read.

## Collection Behavior

- Store-page collection activates one card trend at a time, follows its visible "More" action, verifies the chart product ID, then samples the latest seven visible chart positions.
- Complete store-page histories are saved as direct IXSPY points and skip the corresponding product-page task; incomplete histories retain their observed points and use the existing product-page fallback.
- It does not open the full-year view or collect the cumulative-total series.
- If the series is stale or incomplete after one retry, the snapshot is marked partial and missing dates remain missing.
- If IXSPY requires login, collection pauses and brings the normal login page to the user. Platform verification is always completed by the user.
- The API and dashboard expose only `ixspy_visible_daily_increment` product metrics and their direct store aggregates.
- Existing snapshots and old cumulative-derived metrics are retained, but old estimates are not included in current dashboard or daily-metric API results.

## Limitations

IXSPY's visible chart can be unavailable, delayed, or incomplete. A product may therefore have partial history even when its chart appears populated at a glance. The monitor reports observed coverage rather than inventing values. Any claim of 7/7 refers to the seven expected calendar dates for the collection target date, not to all data shown by the chart.
