from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    Product,
    ProductDailyMetric,
    ProductSnapshot,
    Store,
    StoreDailyMetric,
)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _metric_date(value: datetime, timezone_name: str) -> date:
    return _aware(value).astimezone(ZoneInfo(timezone_name)).date()


def _upsert_product_metric(
    db: Session,
    product_id: int,
    metric_date: date,
    start: ProductSnapshot | None,
    end: ProductSnapshot,
    estimated_sales: int | None,
    reason: str,
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
    metric.start_snapshot_id = start.id if start else None
    metric.end_snapshot_id = end.id
    metric.estimated_sales = estimated_sales
    metric.is_estimable = estimated_sales is not None
    metric.reason = reason
    metric.calculation_method = "public_cumulative_sold_delta"
    return metric


def calculate_product_daily_metrics(
    db: Session, product_id: int, timezone_name: str = "Asia/Shanghai"
) -> list[ProductDailyMetric]:
    snapshots = list(
        db.scalars(
            select(ProductSnapshot)
            .where(ProductSnapshot.product_id == product_id)
            .order_by(ProductSnapshot.collected_at, ProductSnapshot.id)
        )
    )
    representatives: dict[date, ProductSnapshot] = {}
    for snapshot in snapshots:
        representatives[_metric_date(snapshot.collected_at, timezone_name)] = snapshot

    days = sorted(representatives)
    if not days:
        return []
    metrics: list[ProductDailyMetric] = []
    if len(days) == 1:
        only_day = days[0]
        metrics.append(
            _upsert_product_metric(
                db,
                product_id,
                only_day,
                None,
                representatives[only_day],
                None,
                "no_previous_snapshot",
            )
        )
    for index in range(1, len(days)):
        start_day = days[index - 1]
        end_day = days[index]
        start = representatives[start_day]
        end = representatives[end_day]
        if (end_day - start_day).days != 1:
            estimated_sales, reason = None, "non_consecutive_days"
        elif start.parse_status == "failed" or end.parse_status == "failed":
            estimated_sales, reason = None, "snapshot_parse_failed"
        elif start.cumulative_sold is None or end.cumulative_sold is None:
            estimated_sales, reason = None, "cumulative_sold_missing"
        elif end.cumulative_sold < start.cumulative_sold:
            estimated_sales, reason = None, "cumulative_sold_decreased"
        else:
            estimated_sales = end.cumulative_sold - start.cumulative_sold
            reason = "estimated_from_public_cumulative_delta"
        metrics.append(
            _upsert_product_metric(
                db, product_id, start_day, start, end, estimated_sales, reason
            )
        )
    db.commit()
    for metric in metrics:
        db.refresh(metric)
    return metrics


def calculate_store_daily_metric(
    db: Session, store_id: int, metric_date: date
) -> StoreDailyMetric:
    active_product_ids = list(
        db.scalars(
            select(Product.id).where(
                Product.store_id == store_id,
                Product.status == "active",
            )
        )
    )
    metrics_by_product: dict[int, ProductDailyMetric] = {}
    if active_product_ids:
        metrics_by_product = {
            metric.product_id: metric
            for metric in db.scalars(
                select(ProductDailyMetric).where(
                    ProductDailyMetric.product_id.in_(active_product_ids),
                    ProductDailyMetric.metric_date == metric_date,
                )
            )
        }
    estimable = [
        metrics_by_product[product_id]
        for product_id in active_product_ids
        if product_id in metrics_by_product and metrics_by_product[product_id].is_estimable
    ]
    unavailable_count = len(active_product_ids) - len(estimable)
    aggregate = db.scalar(
        select(StoreDailyMetric).where(
            StoreDailyMetric.store_id == store_id,
            StoreDailyMetric.metric_date == metric_date,
        )
    )
    if aggregate is None:
        aggregate = StoreDailyMetric(store_id=store_id, metric_date=metric_date)
        db.add(aggregate)
    aggregate.estimated_sales = sum(metric.estimated_sales or 0 for metric in estimable)
    aggregate.estimable_products = len(estimable)
    aggregate.unavailable_products = unavailable_count
    aggregate.product_scope_count = len(active_product_ids)
    aggregate.calculation_method = "sum_of_monitored_product_estimates"
    db.commit()
    db.refresh(aggregate)
    return aggregate


def calculate_all_store_metrics(
    db: Session, metric_dates: set[date]
) -> list[StoreDailyMetric]:
    store_ids = list(db.scalars(select(Store.id).where(Store.status == "active")))
    return [
        calculate_store_daily_metric(db, store_id, metric_date)
        for metric_date in sorted(metric_dates)
        for store_id in store_ids
    ]
