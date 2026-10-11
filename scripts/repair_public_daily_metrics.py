from __future__ import annotations

import argparse
import json

from sqlalchemy import select

from app.db.models import Product, ProductDailyMetric, StoreDailyMetric
from app.db.session import get_session_factory
from app.services.metrics import calculate_product_daily_metrics, calculate_store_daily_metric


def metric_state(metric: ProductDailyMetric | StoreDailyMetric | None) -> tuple | None:
    if metric is None:
        return None
    if isinstance(metric, ProductDailyMetric):
        return (
            metric.estimated_sales,
            metric.is_estimable,
            metric.reason,
            metric.calculation_method,
            metric.start_snapshot_id,
            metric.end_snapshot_id,
        )
    return (
        metric.estimated_sales,
        metric.estimable_products,
        metric.unavailable_products,
        metric.product_scope_count,
        metric.calculation_method,
    )


def repair_public_metrics(session, product_id: int | None = None) -> dict:
    product_ids = select(ProductDailyMetric.product_id).where(
        ProductDailyMetric.calculation_method.in_(
            {"public_cumulative_sold_delta", "ixspy_visible_cumulative_delta"}
        )
    ).distinct()
    if product_id is not None:
        product_ids = product_ids.where(ProductDailyMetric.product_id == product_id)

    changed_products: list[tuple[int, int, object]] = []
    for candidate_id in session.scalars(product_ids):
        product = session.get(Product, candidate_id)
        if product is None:
            continue
        before = {
            metric.metric_date: metric_state(metric)
            for metric in session.scalars(
                select(ProductDailyMetric).where(ProductDailyMetric.product_id == candidate_id)
            )
        }
        recalculated = calculate_product_daily_metrics(session, candidate_id, commit=False)
        for metric in recalculated:
            if before.get(metric.metric_date) != metric_state(metric):
                changed_products.append((product.store_id, candidate_id, metric.metric_date))

    changed_store_metrics = 0
    affected_store_dates = {(store_id, metric_date) for store_id, _, metric_date in changed_products}
    for store_id, metric_date in sorted(affected_store_dates):
        previous = session.scalar(
            select(StoreDailyMetric).where(
                StoreDailyMetric.store_id == store_id,
                StoreDailyMetric.metric_date == metric_date,
            )
        )
        previous_state = metric_state(previous)
        updated = calculate_store_daily_metric(session, store_id, metric_date, commit=False)
        changed_store_metrics += previous_state != metric_state(updated)

    session.flush()
    return {
        "products_with_changed_metrics": len({product for _, product, _ in changed_products}),
        "product_metrics_changed": len(changed_products),
        "store_dates_recalculated": len(affected_store_dates),
        "store_metrics_changed": changed_store_metrics,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Recalculate public-derived sales metrics without editing snapshots."
    )
    parser.add_argument("--apply", action="store_true", help="Commit; defaults to a dry run.")
    parser.add_argument("--product-id", type=int, help="Limit the repair to one product record.")
    args = parser.parse_args()

    with get_session_factory()() as session:
        result = repair_public_metrics(session, args.product_id)
        if args.apply:
            session.commit()
        else:
            session.rollback()

    print(json.dumps({**result, "applied": args.apply}))


if __name__ == "__main__":
    main()
