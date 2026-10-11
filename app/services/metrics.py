from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    Product,
    ProductDailyMetric,
    Store,
    StoreDailyMetric,
)


def calculate_product_daily_metrics(
    db: Session,
    product_id: int,
    timezone_name: str = "Asia/Shanghai",
    *,
    commit: bool = True,
) -> list[ProductDailyMetric]:
    metrics = list(
        db.scalars(
            select(ProductDailyMetric)
            .where(
                ProductDailyMetric.product_id == product_id,
                ProductDailyMetric.calculation_method == "ixspy_visible_daily_increment",
            )
            .order_by(ProductDailyMetric.metric_date)
        )
    )
    if commit:
        db.commit()
    else:
        db.flush()
    return metrics


def calculate_store_daily_metric(
    db: Session, store_id: int, metric_date: date, *, commit: bool = True
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
                    ProductDailyMetric.calculation_method == "ixspy_visible_daily_increment",
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
    aggregate.calculation_method = "sum_of_ixspy_visible_daily_increments"
    if commit:
        db.commit()
        db.refresh(aggregate)
    else:
        db.flush()
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
