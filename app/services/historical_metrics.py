from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.schemas import HistoricalSalesPoint
from app.db.models import ProductDailyMetric, ProductSnapshot


def _upsert_imported_metric(
    db: Session,
    *,
    product_id: int,
    metric_date: date,
    value: int,
    snapshot_id: int,
    reason: str,
    calculation_method: str,
) -> ProductDailyMetric:
    metric = db.scalar(
        select(ProductDailyMetric).where(
            ProductDailyMetric.product_id == product_id,
            ProductDailyMetric.metric_date == metric_date,
        )
    )
    if metric is None:
        metric = ProductDailyMetric(product_id=product_id, metric_date=metric_date)
        db.add(metric)
    metric.estimated_sales = value
    metric.is_estimable = True
    metric.reason = reason
    metric.start_snapshot_id = None
    metric.end_snapshot_id = snapshot_id
    metric.calculation_method = calculation_method
    metric.calculated_at = datetime.now(timezone.utc)
    return metric


def import_historical_sales(
    db: Session,
    product_id: int,
    snapshot_id: int,
    points: list[HistoricalSalesPoint],
    *,
    commit: bool = False,
) -> set[date]:
    """Import only explicitly typed, visible historical sales points.

    Cumulative values are converted only across consecutive dates and only when
    the value does not decrease. Invalid baselines remain in raw Snapshot data
    but never become fabricated daily sales.
    """
    if not points:
        return set()

    by_date = {point.date: point for point in points}
    ordered = [by_date[key] for key in sorted(by_date)]
    touched: set[date] = set()
    for point in ordered:
        if point.value_type == "daily_increment":
            _upsert_imported_metric(
                db,
                product_id=product_id,
                metric_date=point.date,
                value=point.value,
                snapshot_id=snapshot_id,
                reason="ixspy_visible_daily_increment",
                calculation_method="ixspy_visible_daily_increment",
            )
            touched.add(point.date)

    # Keep the last visible cumulative values in raw snapshots as a durable
    # baseline. This lets the next run submit only the newly observed date.
    known_cumulative: dict[date, int] = {}
    previous_payloads = db.scalars(
        select(ProductSnapshot.raw_data)
        .where(ProductSnapshot.product_id == product_id)
        .order_by(ProductSnapshot.captured_at.desc(), ProductSnapshot.id.desc())
    ).all()
    for raw_data in previous_payloads:
        if not isinstance(raw_data, dict):
            continue
        for raw_point in raw_data.get("historical_sales", []):
            if not isinstance(raw_point, dict) or raw_point.get("value_type") != "cumulative_total":
                continue
            try:
                raw_date = date.fromisoformat(str(raw_point["date"]))
                raw_value = int(raw_point["value"])
            except (KeyError, TypeError, ValueError):
                continue
            known_cumulative.setdefault(raw_date, raw_value)

    cumulative = [point for point in ordered if point.value_type == "cumulative_total"]
    for current in cumulative:
        previous = known_cumulative.get(current.date - timedelta(days=1))
        known_cumulative[current.date] = current.value
        if previous is None or current.value < previous:
            continue
        _upsert_imported_metric(
            db,
            product_id=product_id,
            metric_date=current.date,
            value=current.value - previous,
            snapshot_id=snapshot_id,
            reason="ixspy_visible_cumulative_delta",
            calculation_method="ixspy_visible_cumulative_delta",
        )
        touched.add(current.date)

    if commit:
        db.commit()
    else:
        db.flush()
    return touched
