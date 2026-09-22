from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.schemas import HistoricalSalesPoint
from app.db.models import ProductDailyMetric


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

    cumulative = [point for point in ordered if point.value_type == "cumulative_total"]
    for previous, current in zip(cumulative, cumulative[1:]):
        if (current.date - previous.date).days != 1:
            continue
        if current.value < previous.value:
            continue
        _upsert_imported_metric(
            db,
            product_id=product_id,
            metric_date=current.date,
            value=current.value - previous.value,
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
