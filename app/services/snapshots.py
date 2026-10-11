from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.collectors.base import Collector, CollectorResult, CollectorTarget
from app.api.schemas import HistoricalSalesPoint
from app.db.models import CollectionAttempt, ManualCollectionTask, Product, ProductSnapshot
from app.services.product_presentation import clean_product_title


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
    if product.title:
        product.title = clean_product_title(product.title)
    elif result.title:
        product.title = clean_product_title(result.title)
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
    historical_sales: list[HistoricalSalesPoint] | None = None,
    history_target_date: date | None = None,
    history_source: str | None = None,
    commit: bool = False,
) -> ProductSnapshot:
    """Persist store-page metadata and only explicitly read IXSPY daily points."""
    _ = sold_count  # Accepted for compatibility; cumulative totals are no longer stored.
    expected_dates = (
        {history_target_date - timedelta(days=offset) for offset in range(1, 8)}
        if history_target_date
        else set()
    )
    points_by_date = {
        point.date: point
        for point in historical_sales or []
        if point.value_type == "daily_increment" and point.date in expected_dates
    }
    complete = (
        bool(expected_dates)
        and history_source == "ixspy_visible_daily_increment"
        and set(points_by_date) == expected_dates
    )
    extension_metadata = raw_payload.get("extension_raw_data")
    payload = {
        **raw_payload,
        "historical_sales": [
            point.model_dump(mode="json")
            for _, point in sorted(points_by_date.items())
        ],
        "history_source": (
            "ixspy_visible_daily_increment"
            if points_by_date and history_source == "ixspy_visible_daily_increment"
            else "ixspy_daily_increment_unavailable"
        ),
        "history_target_date": history_target_date.isoformat() if history_target_date else None,
        "history_expected_dates": sorted(value.isoformat() for value in expected_dates),
        "history_missing_dates": sorted(
            value.isoformat() for value in expected_dates - set(points_by_date)
        ),
        "history_daily_capture_complete": complete,
        "history_sampling_strategy": "store_card_increment_chart_latest_7_dates",
        "history_collector_version": (
            extension_metadata.get("history_collector_version")
            if isinstance(extension_metadata, dict)
            else None
        ),
    }
    result = CollectorResult(
        collected_at=captured_at,
        collector_name=collector_name,
        collector_version=collector_version,
        parse_status="success" if complete else "partial",
        cumulative_sold=None,
        title=clean_product_title(title),
        source_http_status=200,
        raw_payload=payload,
        error_type=None if complete else "ixspy_daily_increment_unavailable",
        error_message=None if complete else (
            f"店铺页 IXSPY 最近 7 日仅读取到 {len(points_by_date)}/7 天日增量"
        ),
    )
    return _save_snapshot_result(db, product, result, source, commit=commit)
