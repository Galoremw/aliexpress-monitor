from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.schemas import BrowserStoreProduct
from app.api.utils import extract_product_id, normalize_aliexpress_url
from app.db.models import (
    Product,
    Store,
    StorePoolResetBatch,
    StorePoolResetStore,
)
from app.services.historical_metrics import (
    import_historical_sales,
    looks_cumulative_like_daily_values,
)
from app.services.metrics import calculate_product_daily_metrics, calculate_store_daily_metric
from app.services.product_presentation import clean_product_title
from app.services.snapshots import save_store_product_snapshot


@dataclass(frozen=True)
class RankedPoolResetCandidate:
    product: BrowserStoreProduct
    orders_rank: int
    page_url: str


def rank_pool_reset_candidates(
    candidates: list[RankedPoolResetCandidate],
    window_start: date,
    window_end: date,
) -> tuple[list[RankedPoolResetCandidate], list[dict]]:
    if (window_end - window_start).days != 6:
        raise ValueError("Pool reset window must contain exactly seven dates")

    expected_dates = {
        window_start + timedelta(days=offset)
        for offset in range(7)
    }
    seen_product_ids: set[str] = set()
    eligible: list[tuple[RankedPoolResetCandidate, int]] = []
    diagnostics: list[dict] = []

    for candidate in candidates:
        product = candidate.product
        product_id = product.platform_product_id
        reason = None
        if product_id in seen_product_ids:
            reason = "duplicate_product_id"
        else:
            seen_product_ids.add(product_id)

        values_by_date: dict[date, int] = {}
        duplicate_date = False
        if reason is None:
            if product.history_source != "ixspy_visible_daily_increment":
                reason = "history_source_untrusted"
            elif not product.history_daily_capture_complete:
                reason = "history_capture_incomplete"
            else:
                for point in product.historical_sales:
                    if point.date not in expected_dates:
                        continue
                    if point.value_type != "daily_increment" or point.date in values_by_date:
                        duplicate_date = True
                        continue
                    values_by_date[point.date] = point.value
                if duplicate_date or set(values_by_date) != expected_dates:
                    reason = "seven_day_history_incomplete"
                elif looks_cumulative_like_daily_values(
                    [values_by_date[day] for day in sorted(expected_dates)]
                ):
                    reason = "cumulative_like_history"

        if reason is not None:
            diagnostics.append({
                "product_id": product_id,
                "orders_rank": candidate.orders_rank,
                "reason": reason,
            })
        else:
            eligible.append((candidate, sum(values_by_date.values())))

    eligible.sort(
        key=lambda row: (
            -row[1],
            row[0].orders_rank,
            row[0].product.platform_product_id,
        )
    )
    selected = eligible[:20]
    diagnostics.extend(
        {
            "product_id": candidate.product.platform_product_id,
            "orders_rank": candidate.orders_rank,
            "reason": "rank_limit_exceeded",
        }
        for candidate, _ in eligible[20:]
    )
    return [candidate for candidate, _ in selected], diagnostics


def replace_store_pool_for_store(
    db: Session,
    batch: StorePoolResetBatch,
    store_task: StorePoolResetStore,
    selected_candidates: list[RankedPoolResetCandidate],
) -> dict:
    store = db.scalar(
        select(Store)
        .where(Store.id == store_task.store_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ) if store_task.store_id is not None else None
    if store is None or store.status != "active":
        return {"skipped": True, "reason": "store_inactive_or_missing", "selected_count": 0}

    ranked, diagnostics = rank_pool_reset_candidates(
        selected_candidates, batch.window_start, batch.window_end
    )
    if len(ranked) > 20:
        raise ValueError("A store pool reset cannot select more than 20 products")

    selected_ids: set[str] = set()
    resolved: list[tuple[RankedPoolResetCandidate, str, Product | None]] = []
    for candidate in ranked:
        source = candidate.product
        product_id = source.platform_product_id
        try:
            product_url = normalize_aliexpress_url(source.url, "product")
        except Exception:
            diagnostics.append({
                "product_id": product_id,
                "orders_rank": candidate.orders_rank,
                "reason": "invalid_product_url",
            })
            continue
        if extract_product_id(product_url) != product_id:
            diagnostics.append({
                "product_id": product_id,
                "orders_rank": candidate.orders_rank,
                "reason": "product_url_id_mismatch",
            })
            continue

        id_owner = db.scalar(
            select(Product).where(
                Product.aliexpress_product_id == product_id,
                Product.store_id != store.id,
            )
        )
        if id_owner is not None:
            diagnostics.append({
                "product_id": product_id,
                "orders_rank": candidate.orders_rank,
                "reason": "product_id_owned_by_other_store",
            })
            continue

        url_owner = db.scalar(select(Product).where(Product.url == product_url))
        if url_owner is not None and url_owner.store_id != store.id:
            diagnostics.append({
                "product_id": product_id,
                "orders_rank": candidate.orders_rank,
                "reason": "product_url_owned_by_other_store",
            })
            continue
        if url_owner is not None and url_owner.aliexpress_product_id != product_id:
            diagnostics.append({
                "product_id": product_id,
                "orders_rank": candidate.orders_rank,
                "reason": "product_url_id_conflict",
            })
            continue

        product = db.scalar(
            select(Product).where(
                Product.store_id == store.id,
                Product.aliexpress_product_id == product_id,
            )
        )
        resolved.append((candidate, product_url, product))
        selected_ids.add(product_id)

    now = datetime.now(timezone.utc)
    current_products = list(
        db.scalars(select(Product).where(Product.store_id == store.id).with_for_update())
    )
    deactivated_ids: list[str] = []
    for product in current_products:
        if product.status == "active" and product.aliexpress_product_id not in selected_ids:
            product.status = "inactive"
            deactivated_ids.append(product.aliexpress_product_id)

    for candidate, product_url, product in resolved:
        source = candidate.product
        if product is None:
            product = Product(
                store_id=store.id,
                aliexpress_product_id=source.platform_product_id,
                url=product_url,
                discovery_source="chrome_extension_pool_reset",
                discovered_at=now,
            )
            db.add(product)
        product.status = "active"
        product.url = product_url
        product.title = clean_product_title(source.title) if source.title else product.title
        product.discovery_source = "chrome_extension_pool_reset"
        product.discovery_rank = candidate.orders_rank
        if product.discovered_at is None:
            product.discovered_at = now
        db.flush()
        snapshot = save_store_product_snapshot(
            db,
            product,
            captured_at=now,
            sold_count=None,
            title=source.title,
            source="CHROME_EXTENSION",
            collector_name="chrome_extension_pool_reset",
            collector_version="store-pool-reset-v1",
            raw_payload={
                "collection_mode": "monitored_store_pool_reset",
                "batch_id": batch.id,
                "orders_rank": candidate.orders_rank,
                "page_url": candidate.page_url,
                "product_id": source.platform_product_id,
                "url": product.url,
                "title": source.title,
                "main_image_url": source.image_url,
            },
            historical_sales=source.historical_sales,
            history_target_date=batch.target_date,
            history_source=source.history_source,
            commit=False,
        )
        import_historical_sales(
            db,
            product.id,
            snapshot.id,
            source.historical_sales,
            target_date=batch.target_date,
            commit=False,
        )
        calculate_product_daily_metrics(db, product.id, commit=False)

    for offset in range(1, 8):
        calculate_store_daily_metric(
            db, store.id, batch.target_date - timedelta(days=offset), commit=False
        )

    db.flush()
    return {
        "skipped": False,
        "selected_count": len(resolved),
        "selected_ids": [candidate.product.platform_product_id for candidate, _, _ in resolved],
        "deactivated_ids": sorted(deactivated_ids),
        "diagnostics": diagnostics,
    }
