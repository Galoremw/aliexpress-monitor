from datetime import datetime, timezone

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.collectors.base import Collector, CollectorResult, CollectorTarget
from app.db.models import CollectionAttempt, ManualCollectionTask, Product, ProductSnapshot


def snapshot_status(parse_status: str) -> str:
    return {
        "success": "VALID",
        "partial": "SUSPECT",
        "failed": "FAILED",
    }.get(parse_status, "FAILED")


def _sync_manual_task(
    db: Session,
    product_id: int,
    source: str,
    status: str,
    attempt: CollectionAttempt,
) -> None:
    task = db.scalar(
        select(ManualCollectionTask).where(ManualCollectionTask.product_id == product_id)
    )
    if status == "FAILED" and source == "AUTO":
        if task is None:
            task = ManualCollectionTask(
                product_id=product_id,
                reason=attempt.error_type or "auto_collection_failed",
            )
            db.add(task)
        task.status = "PENDING"
        task.reason = attempt.error_type or "auto_collection_failed"
        task.last_attempt = attempt
        task.completed_at = None
    elif source in {"CHROME_EXTENSION", "MANUAL", "API"} and status != "FAILED":
        if task is not None:
            task.status = "COMPLETED"
            task.last_attempt = attempt
            task.completed_at = attempt.attempted_at


def _save_snapshot_result(
    db: Session,
    product: Product,
    result: CollectorResult,
    source: str,
    *,
    commit: bool = True,
) -> ProductSnapshot:
    status = snapshot_status(result.parse_status)
    captured_at = result.collected_at or datetime.now(timezone.utc)
    snapshot = ProductSnapshot(
        product_id=product.id,
        collected_at=captured_at,
        captured_at=captured_at,
        collector_name=result.collector_name,
        collector_version=result.collector_version,
        parse_status=result.parse_status,
        cumulative_sold=result.cumulative_sold,
        sold_count=result.cumulative_sold,
        price_amount=result.price_amount,
        price=result.price_amount,
        price_currency=result.price_currency,
        review_count=result.review_count,
        title=result.title,
        source_http_status=result.source_http_status,
        raw_payload=result.raw_payload,
        raw_data=result.raw_payload,
        raw_content=result.raw_content,
        error_type=result.error_type,
        error_message=result.error_message,
        source=source,
        status=status,
    )
    db.add(snapshot)
    if result.title and not product.title:
        product.title = result.title
    db.flush()
    attempt = CollectionAttempt(
        product_id=product.id,
        attempted_at=captured_at,
        source=source,
        status=status,
        error_type=result.error_type,
        error_message=result.error_message,
        snapshot_id=snapshot.id,
    )
    db.add(attempt)
    db.flush()
    _sync_manual_task(db, product.id, source, status, attempt)
    if commit:
        db.commit()
        db.refresh(snapshot)
    else:
        db.flush()
    return snapshot


def collect_product_snapshot(
    db: Session, product: Product, collector: Collector, source: str = "AUTO"
) -> ProductSnapshot:
    target = CollectorTarget(
        product_id=product.aliexpress_product_id,
        url=f"https://www.aliexpress.com/item/{product.aliexpress_product_id}.html",
    )
    try:
        result = collector.collect(target)
    except Exception as exc:
        result = CollectorResult(
            collected_at=datetime.now(timezone.utc),
            collector_name=getattr(collector, "name", type(collector).__name__),
            collector_version=getattr(collector, "version", "unknown"),
            parse_status="failed",
            raw_payload={"target": {"product_id": target.product_id, "url": target.url}},
            error_type="collector_exception",
            error_message=str(exc),
        )
    return _save_snapshot_result(db, product, result, source)


def save_external_snapshot(
    db: Session,
    product: Product,
    result: CollectorResult,
    source: str,
    *,
    commit: bool = True,
) -> ProductSnapshot:
    """Persist an observation from a non-HTTP channel through the same path."""
    return _save_snapshot_result(db, product, result, source, commit=commit)


def save_store_product_snapshot(
    db: Session,
    product: Product,
    *,
    captured_at: datetime,
    sold_count: int | None,
    title: str | None,
    source: str,
    collector_name: str,
    collector_version: str,
    raw_payload: dict[str, Any],
    commit: bool = False,
) -> ProductSnapshot:
    """Persist the public cumulative sales observed on a store listing page.

    Store pages generally expose only a subset of product fields.  A sold count
    is enough for the daily delta calculation, while missing counts remain a
    suspect snapshot and can be sent to the product-page fallback queue.
    """
    has_sold_count = sold_count is not None
    result = CollectorResult(
        collected_at=captured_at,
        collector_name=collector_name,
        collector_version=collector_version,
        parse_status="success" if has_sold_count else "partial",
        cumulative_sold=sold_count,
        title=title,
        source_http_status=200,
        raw_payload=raw_payload,
        error_type=None if has_sold_count else "missing_sold_count",
        error_message=None if has_sold_count else "店铺页未提供累计销量",
    )
    return _save_snapshot_result(db, product, result, source, commit=commit)
