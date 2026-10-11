from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.schemas import HistoricalSalesPoint
from app.db.models import ProductDailyMetric, ProductSnapshot


def looks_cumulative_like_daily_values(values: list[int]) -> bool:
    if len(values) != 7:
        return False
    minimum = min(values)
    maximum = max(values)
    return minimum >= 1000 and 10 * (maximum - minimum) <= maximum


def rolling_history_coverage(
    points: list[object], target_date: date, *, sold_count: int | None = None
) -> int:
    """Count directly observed daily-increment points before target_date."""
    first_date = target_date - timedelta(days=7)
    daily_dates: set[date] = set()
    for point in points:
        if isinstance(point, dict):
            raw_date = point.get("date")
            value_type = point.get("value_type")
            raw_value = point.get("value")
        else:
            raw_date = getattr(point, "date", None)
            value_type = getattr(point, "value_type", None)
            raw_value = getattr(point, "value", None)
        if value_type != "daily_increment":
            continue
        try:
            metric_date = raw_date if isinstance(raw_date, date) else date.fromisoformat(str(raw_date))
            value = int(raw_value)
        except (TypeError, ValueError):
            continue
        if value_type == "daily_increment":
            if first_date <= metric_date < target_date:
                daily_dates.add(metric_date)
    return len(daily_dates)


def ixspy_daily_metric_coverage(db: Session, product_id: int, target_date: date) -> int:
    """Count directly observed IXSPY daily increments before target_date."""
    first_date = target_date - timedelta(days=7)
    metrics = db.scalars(
        select(ProductDailyMetric).where(
            ProductDailyMetric.product_id == product_id,
            ProductDailyMetric.metric_date >= first_date,
            ProductDailyMetric.metric_date < target_date,
            ProductDailyMetric.is_estimable.is_(True),
            ProductDailyMetric.estimated_sales.is_not(None),
            ProductDailyMetric.calculation_method == "ixspy_visible_daily_increment",
        )
    )
    return len({metric.metric_date for metric in metrics})


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


def _complete_authoritative_ixspy_points(
    snapshot: ProductSnapshot, expected_dates: set[date]
) -> dict[date, int] | None:
    raw_data = snapshot.raw_data if isinstance(snapshot.raw_data, dict) else {}
    version = raw_data.get("history_collector_version") or snapshot.collector_version or ""
    version_match = re.fullmatch(r"extension-content-(\d+)\.(\d+)\.(\d+)", version)
    store_version_match = re.fullmatch(r"extension-store-(\d+)\.(\d+)\.(\d+)", version)
    if not (
        version_match and tuple(map(int, version_match.groups())) >= (0, 6, 38)
        or store_version_match and tuple(map(int, store_version_match.groups())) >= (0, 4, 0)
    ):
        return None
    if (
        raw_data.get("history_source") != "ixspy_visible_daily_increment"
        or raw_data.get("history_daily_capture_complete") is not True
    ):
        return None
    values: dict[date, int] = {}
    for point in raw_data.get("historical_sales", []):
        if not isinstance(point, dict) or point.get("value_type") != "daily_increment":
            continue
        try:
            metric_date = date.fromisoformat(str(point.get("date")))
            value = int(point.get("value"))
        except (TypeError, ValueError):
            continue
        if metric_date not in expected_dates:
            continue
        if metric_date in values:
            return None
        values[metric_date] = value
    if set(values) != expected_dates or looks_cumulative_like_daily_values(
        [values[day] for day in sorted(expected_dates)]
    ):
        return None
    return values


def repair_recent_ixspy_daily_metrics(
    db: Session,
    *,
    product_ids: list[int],
    target_date: date,
    commit: bool = False,
) -> dict[str, int]:
    """Restore derived metrics from complete, current IXSPY daily snapshots only."""
    from app.db.models import Product
    from app.services.metrics import calculate_store_daily_metric

    expected_dates = {target_date - timedelta(days=offset) for offset in range(1, 8)}
    repaired_products = 0
    repaired_metrics = 0
    store_dates: set[tuple[int, date]] = set()
    for product_id in dict.fromkeys(product_ids):
        product = db.get(Product, product_id)
        if product is None:
            continue
        snapshots = db.scalars(
            select(ProductSnapshot)
            .where(ProductSnapshot.product_id == product_id)
            .order_by(ProductSnapshot.captured_at.desc(), ProductSnapshot.id.desc())
        )
        snapshot_and_points = next(
            (
                (snapshot, points)
                for snapshot in snapshots
                if (points := _complete_authoritative_ixspy_points(snapshot, expected_dates)) is not None
            ),
            None,
        )
        if snapshot_and_points is None:
            continue
        snapshot, points = snapshot_and_points
        repaired_products += 1
        for metric_date, value in points.items():
            _upsert_imported_metric(
                db,
                product_id=product_id,
                metric_date=metric_date,
                value=value,
                snapshot_id=snapshot.id,
                reason="ixspy_visible_daily_increment",
                calculation_method="ixspy_visible_daily_increment",
            )
            repaired_metrics += 1
            store_dates.add((product.store_id, metric_date))

    db.flush()
    for store_id, metric_date in store_dates:
        calculate_store_daily_metric(db, store_id, metric_date, commit=False)
    if commit:
        db.commit()
    else:
        db.flush()
    return {"products": repaired_products, "metrics": repaired_metrics}


def import_historical_sales(
    db: Session,
    product_id: int,
    snapshot_id: int,
    points: list[HistoricalSalesPoint],
    *,
    target_date: date | None = None,
    commit: bool = False,
) -> set[date]:
    """Import only explicitly typed, visible IXSPY daily-increment points."""
    if not points:
        if commit:
            db.commit()
        else:
            db.flush()
        return set()

    touched: set[date] = set()

    first_day = target_date - timedelta(days=7) if target_date else None
    daily_candidates: dict[date, HistoricalSalesPoint] = {}
    for point in points:
        if point.value_type != "daily_increment":
            continue
        if target_date and not first_day <= point.date < target_date:
            continue
        daily_candidates[point.date] = point
    for point_date, point in sorted(daily_candidates.items()):
        _upsert_imported_metric(
            db,
            product_id=product_id,
            metric_date=point_date,
            value=point.value,
            snapshot_id=snapshot_id,
            reason="ixspy_visible_daily_increment",
            calculation_method="ixspy_visible_daily_increment",
        )
        touched.add(point_date)

    if commit:
        db.commit()
    else:
        db.flush()
    return touched
