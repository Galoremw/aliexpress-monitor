from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from sqlalchemy import case, select
from sqlalchemy.orm import Session, joinedload, selectinload

from app.db.models import (
    DianxiaomiHandoff,
    Product,
    ProductDailyMetric,
    SelectionCandidate,
    SelectionCandidateImport,
    SelectionQualificationRule,
    Store,
)
from app.services.product_presentation import clean_product_title


SELECTION_CANDIDATE_SCOPE = "selection_candidate"
_PRODUCT_PATH = re.compile(r"^/(?:us/)?item/(\d+)(?:\.html)?/?$", re.IGNORECASE)


def normalize_selection_candidate_url(url: str, product_id: str) -> str:
    parsed = urlsplit(url.strip())
    host = (parsed.hostname or "").lower()
    valid_host = any(
        host == domain or host.endswith(f".{domain}")
        for domain in ("aliexpress.com", "aliexpress.us")
    )
    if parsed.scheme.lower() != "https" or not valid_host:
        raise ValueError("Only HTTPS AliExpress product URLs are accepted")
    match = _PRODUCT_PATH.fullmatch(parsed.path)
    if not match or match.group(1) != product_id:
        raise ValueError("AliExpress URL must identify the submitted product ID")
    return f"https://www.aliexpress.com/item/{product_id}.html"


def get_selection_rule(db: Session) -> SelectionQualificationRule | None:
    return db.get(SelectionQualificationRule, 1)


def set_selection_rule(
    db: Session,
    minimum_7d_increment_total: int | None,
    minimum_positive_days: int | None,
) -> SelectionQualificationRule:
    row = get_selection_rule(db)
    if row is None:
        row = SelectionQualificationRule(
            id=1,
            minimum_7d_increment_total=minimum_7d_increment_total,
            minimum_positive_days=minimum_positive_days,
        )
        db.add(row)
    else:
        row.minimum_7d_increment_total = minimum_7d_increment_total
        row.minimum_positive_days = minimum_positive_days
    db.flush()
    return row


def _candidate_store(db: Session) -> Store:
    store = db.scalar(
        select(Store).where(
            Store.name == "自定义监控",
            Store.aliexpress_store_id.is_(None),
        )
    )
    if store is None:
        # Reuse the existing local-only custom-monitor store if it owns the root URL.
        store = db.scalar(
            select(Store).where(
                Store.url == "https://www.aliexpress.com/",
                Store.aliexpress_store_id.is_(None),
            )
        )
    if store is None:
        store = Store(
            name="自定义监控",
            url="https://www.aliexpress.com/",
            aliexpress_store_id=None,
        )
        db.add(store)
        db.flush()
    return store


def import_selection_candidates(
    db: Session, batch_id: str, candidates: list[Any]
) -> dict[str, Any]:
    store = _candidate_store(db)
    items: list[dict[str, Any]] = []
    created_product_ids: set[int] = set()
    added_associations = 0
    seen_ids: set[str] = set()

    for item in candidates:
        product_id = item.aliexpress_product_id.strip()
        if item.platform != "aliexpress":
            raise ValueError("Only AliExpress candidates can be imported")
        canonical_url = normalize_selection_candidate_url(item.url, product_id)
        if product_id in seen_ids:
            continue
        seen_ids.add(product_id)

        product = db.scalar(
            select(Product)
            .where(Product.aliexpress_product_id == product_id)
            .order_by(case((Product.status == "active", 0), else_=1), Product.id)
            .limit(1)
        )
        if product is None:
            product = Product(
                store_id=store.id,
                aliexpress_product_id=product_id,
                url=canonical_url,
                title=clean_product_title(item.title),
                discovery_source="selection_workbench",
                monitor_scope=SELECTION_CANDIDATE_SCOPE,
            )
            db.add(product)
            db.flush()
            created_product_ids.add(product.id)
        else:
            product.monitor_scope = SELECTION_CANDIDATE_SCOPE
            product.status = "active"
            if product.title is None and item.title:
                product.title = clean_product_title(item.title)

        candidate = db.scalar(
            select(SelectionCandidate).where(SelectionCandidate.product_id == product.id)
        )
        if candidate is None:
            candidate = SelectionCandidate(product_id=product.id)
            db.add(candidate)
            db.flush()

        association = db.scalar(
            select(SelectionCandidateImport).where(
                SelectionCandidateImport.candidate_id == candidate.id,
                SelectionCandidateImport.batch_id == batch_id,
            )
        )
        association_created = association is None
        if association is None:
            association = SelectionCandidateImport(
                candidate_id=candidate.id,
                batch_id=batch_id,
                source_candidate_id=item.source_candidate_id,
                submitted_url=canonical_url,
                source_metadata={
                    **(dict(item.source_metadata or {})),
                    **({"source": item.source} if item.source else {}),
                },
            )
            db.add(association)
            added_associations += 1
        evaluate_selection_candidate(db, product.id)
        items.append(
            {
                "selection_candidate_id": candidate.id,
                "product_id": product.id,
                "aliexpress_product_id": product.aliexpress_product_id,
                "url": product.url,
                "created": product.id in created_product_ids,
                "batch_association_created": association_created,
            }
        )

    db.commit()
    return {
        "batch_id": batch_id,
        "created_count": len(created_product_ids),
        "associated_count": added_associations,
        "reused_count": max(len(seen_ids) - len(created_product_ids), 0),
        "items": items,
    }


def _selection_candidate_for_product(db: Session, product_id: int) -> SelectionCandidate | None:
    return db.scalar(
        select(SelectionCandidate)
        .options(joinedload(SelectionCandidate.product), joinedload(SelectionCandidate.handoff))
        .where(SelectionCandidate.product_id == product_id)
    )


def evaluate_selection_candidate(
    db: Session,
    product_id: int,
    *,
    as_of_date: date | None = None,
) -> SelectionCandidate | None:
    candidate = _selection_candidate_for_product(db, product_id)
    if candidate is None:
        return None

    timezone_name = "Asia/Shanghai"
    local_date = as_of_date or datetime.now(ZoneInfo(timezone_name)).date()
    window_end = local_date - timedelta(days=1)
    window_start = window_end - timedelta(days=6)
    dates = [window_start + timedelta(days=offset) for offset in range(7)]
    metrics = list(
        db.scalars(
            select(ProductDailyMetric).where(
                ProductDailyMetric.product_id == product_id,
                ProductDailyMetric.metric_date >= window_start,
                ProductDailyMetric.metric_date <= window_end,
                ProductDailyMetric.calculation_method == "ixspy_visible_daily_increment",
                ProductDailyMetric.is_estimable.is_(True),
                ProductDailyMetric.estimated_sales.is_not(None),
            )
        )
    )
    by_date = {metric.metric_date: metric.estimated_sales for metric in metrics}
    daily_values = [by_date.get(metric_date) for metric_date in dates]
    observed_days = sum(value is not None for value in daily_values)
    missing_dates = [
        metric_date.isoformat()
        for metric_date, value in zip(dates, daily_values, strict=True)
        if value is None
    ]
    total_increment = sum(value for value in daily_values if value is not None)
    positive_days = sum(value is not None and value > 0 for value in daily_values)
    rule = get_selection_rule(db)
    minimum_total = rule.minimum_7d_increment_total if rule else None
    minimum_positive_days = rule.minimum_positive_days if rule else None
    evidence = {
        "timezone": timezone_name,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "observed_days": observed_days,
        "missing_dates": missing_dates,
        "daily_values": daily_values,
        "total_increment": total_increment,
        "positive_days": positive_days,
        "minimum_7d_increment_total": minimum_total,
        "minimum_positive_days": minimum_positive_days,
    }

    if minimum_total is None or minimum_positive_days is None:
        candidate.qualification_status = "RULE_UNCONFIGURED"
    elif observed_days < 7:
        candidate.qualification_status = "WAITING_FOR_DATA"
    else:
        qualifies = total_increment >= minimum_total and positive_days >= minimum_positive_days
        candidate.qualification_status = "QUALIFIED" if qualifies else "BELOW_THRESHOLD"
        if qualifies:
            _ensure_candidate_handoff(db, candidate)

    candidate.evidence_json = evidence
    candidate.evaluated_at = datetime.now(timezone.utc)
    db.flush()
    return candidate


def _ensure_candidate_handoff(db: Session, candidate: SelectionCandidate) -> None:
    if candidate.handoff_id is not None:
        return
    product = candidate.product
    batch_id = f"selection-{candidate.id}"
    handoff = db.scalar(
        select(DianxiaomiHandoff)
        .where(
            DianxiaomiHandoff.product_id == product.id,
            DianxiaomiHandoff.batch_id == batch_id,
        )
    )
    if handoff is None:
        submitted_url = db.scalar(
            select(SelectionCandidateImport.submitted_url)
            .where(SelectionCandidateImport.candidate_id == candidate.id)
            .order_by(SelectionCandidateImport.imported_at.desc(), SelectionCandidateImport.id.desc())
            .limit(1)
        )
        handoff = DianxiaomiHandoff(
            batch_id=batch_id,
            product_id=product.id,
            store_id=product.store_id,
            target_url=submitted_url or product.url,
            status="QUEUED",
            attempt_count=0,
        )
        db.add(handoff)
        db.flush()
    candidate.handoff_id = handoff.id


def evaluate_all_selection_candidates(
    db: Session, *, as_of_date: date | None = None
) -> list[SelectionCandidate]:
    candidate_ids = list(db.scalars(select(SelectionCandidate.product_id).order_by(SelectionCandidate.id)))
    return [
        candidate
        for product_id in candidate_ids
        if (candidate := evaluate_selection_candidate(db, product_id, as_of_date=as_of_date))
        is not None
    ]


def serialize_selection_candidate(candidate: SelectionCandidate) -> dict[str, Any]:
    product = candidate.product
    handoff = candidate.handoff
    return {
        "selection_candidate_id": candidate.id,
        "product_id": product.id,
        "aliexpress_product_id": product.aliexpress_product_id,
        "title": product.title,
        "url": product.url,
        "monitor_scope": product.monitor_scope,
        "qualification_status": candidate.qualification_status,
        "evidence": candidate.evidence_json,
        "evaluated_at": candidate.evaluated_at,
        "handoff_id": candidate.handoff_id,
        "handoff_status": handoff.status if handoff else None,
        "attempt_count": handoff.attempt_count if handoff else None,
        "provenance": [
            {
                "batch_id": row.batch_id,
                "source_candidate_id": row.source_candidate_id,
                "submitted_url": row.submitted_url,
                "source_metadata": row.source_metadata,
                "imported_at": row.imported_at,
            }
            for row in candidate.provenance
        ],
    }


def list_selection_candidates(db: Session) -> list[SelectionCandidate]:
    return list(
        db.scalars(
            select(SelectionCandidate)
            .options(
                joinedload(SelectionCandidate.product),
                joinedload(SelectionCandidate.handoff),
                selectinload(SelectionCandidate.provenance),
            )
            .order_by(SelectionCandidate.id.desc())
        )
    )
