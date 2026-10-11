from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.db.models import Product, ProductReview, ReviewCollectionTask, Store, utcnow

ACTIVE_REVIEW_TASK_STATUSES = ("PENDING", "RUNNING", "NEEDS_VERIFICATION")


def _normalized(value: str | None) -> str:
    return " ".join((value or "").split()).casefold()


def review_identity_hash(
    *,
    platform_review_id: str | None,
    review_text: str,
    rating: Decimal | None,
    review_date,
    country_region: str | None,
    variant: str | None,
) -> str:
    if platform_review_id:
        identity = "id\x1f" + platform_review_id.strip()
    else:
        identity = "\x1f".join(
            (
                "fp",
                _normalized(review_text),
                f"{rating:.2f}" if rating is not None else "",
                review_date.isoformat() if review_date else "",
                _normalized(country_region),
                _normalized(variant),
            )
        )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def serialize_task(task: ReviewCollectionTask) -> dict:
    product = task.product
    return {
        "id": task.id,
        "product_id": task.product_id,
        "product_title": product.title,
        "product_url": task.target_url,
        "store_name": product.store.name,
        "status": task.status,
        "max_new_reviews": task.max_new_reviews,
        "attempt_count": task.attempt_count,
        "scanned_count": task.scanned_count,
        "saved_count": task.saved_count,
        "created_at": task.created_at,
        "claimed_at": task.claimed_at,
        "completed_at": task.completed_at,
        "error_type": task.error_type,
        "error_message": task.error_message,
    }


def task_query():
    return select(ReviewCollectionTask).options(
        joinedload(ReviewCollectionTask.product).joinedload(Product.store)
    )


def create_or_reuse_task(db: Session, product: Product, max_new_reviews: int) -> ReviewCollectionTask:
    active = db.scalar(
        task_query()
        .where(
            ReviewCollectionTask.product_id == product.id,
            ReviewCollectionTask.status.in_(ACTIVE_REVIEW_TASK_STATUSES),
        )
        .order_by(ReviewCollectionTask.created_at.desc())
    )
    if active:
        return active
    task = ReviewCollectionTask(
        product_id=product.id,
        target_url=product.url,
        max_new_reviews=max_new_reviews,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return db.scalar(task_query().where(ReviewCollectionTask.id == task.id))


def claim_next_task(db: Session, worker_id: str = "chrome-extension") -> tuple[ReviewCollectionTask | None, list[str]]:
    now = utcnow()
    expired = list(
        db.scalars(
            select(ReviewCollectionTask).where(
                ReviewCollectionTask.status == "RUNNING",
                ReviewCollectionTask.lease_expires_at < now,
            )
        )
    )
    for task in expired:
        task.status = "PENDING"
        task.error_type = "lease_expired"
        task.error_message = "浏览器任务租约过期，已重新排队"
    db.flush()

    task = db.scalar(
        task_query()
        .where(ReviewCollectionTask.status == "PENDING")
        .order_by(ReviewCollectionTask.created_at, ReviewCollectionTask.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if task is None:
        db.commit()
        return None, []

    task.status = "RUNNING"
    task.worker_id = worker_id[:128]
    task.claimed_at = now
    task.lease_expires_at = now + timedelta(minutes=15)
    task.attempt_count += 1
    task.error_type = None
    task.error_message = None
    known_keys: list[str] = []
    for review in db.scalars(
        select(ProductReview)
        .where(ProductReview.product_id == task.product_id)
        .order_by(ProductReview.first_seen_at.desc(), ProductReview.id.desc())
        .limit(2000)
    ):
        if review.platform_review_id:
            known_keys.append(f"id:{review.platform_review_id}")
        else:
            known_keys.append(f"fp:{review.identity_hash}")
    db.commit()
    return task, known_keys


def complete_task(db: Session, task: ReviewCollectionTask, payload) -> ReviewCollectionTask:
    now = utcnow()
    saved = 0
    seen_in_payload: set[str] = set()
    for incoming in payload.reviews:
        identity_hash = review_identity_hash(
            platform_review_id=incoming.platform_review_id,
            review_text=incoming.review_text,
            rating=incoming.rating,
            review_date=incoming.review_date,
            country_region=incoming.country_region,
            variant=incoming.variant,
        )
        if identity_hash in seen_in_payload:
            continue
        seen_in_payload.add(identity_hash)
        review = db.scalar(
            select(ProductReview).where(
                ProductReview.product_id == task.product_id,
                ProductReview.identity_hash == identity_hash,
            )
        )
        if review:
            review.last_seen_at = now
            continue
        db.add(
            ProductReview(
                product_id=task.product_id,
                platform_review_id=incoming.platform_review_id,
                identity_hash=identity_hash,
                review_text=incoming.review_text,
                rating=incoming.rating,
                review_date=incoming.review_date,
                country_region=incoming.country_region,
                variant=incoming.variant,
                has_media=incoming.has_media,
                verified_purchase=incoming.verified_purchase,
                first_seen_at=now,
                last_seen_at=now,
                raw_data=incoming.raw_data,
            )
        )
        saved += 1
    task.status = payload.status
    task.scanned_count = payload.scanned_count
    task.saved_count = saved
    task.completed_at = now
    task.lease_expires_at = None
    task.error_type = payload.error_type
    task.error_message = payload.error_message
    db.commit()
    return db.scalar(task_query().where(ReviewCollectionTask.id == task.id))


_STOPWORDS = {
    "the", "and", "for", "with", "this", "that", "from", "are", "was", "were", "you",
    "your", "have", "has", "had", "but", "not", "very", "it", "its", "is", "to", "of",
    "in", "on", "a", "an", "as", "at", "be", "my", "i", "we", "they", "them", "so",
    "good", "great", "nice", "love", "like", "product", "item", "都", "很", "也", "的", "了",
    "是", "我", "有", "不", "还", "和", "就", "都", "这个", "一个", "没有", "非常",
}


def tokens(value: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+|[\u3400-\u9fff]+", value.casefold())
    return [word for word in words if len(word) > 1 and word not in _STOPWORDS and not word.isdigit()]


def ranked_terms(products: Iterable[Product], source: str, limit: int = 40) -> list[dict]:
    frequencies: dict[str, int] = {}
    product_counts: dict[str, int] = {}
    for product in products:
        text_value = product.title if source == "title" else " ".join(
            review.review_text for review in product.reviews
        )
        words = tokens(text_value or "")
        candidates = set(words)
        candidates.update(f"{left} {right}" for left, right in zip(words, words[1:]) if len(left) > 1 and len(right) > 1)
        for candidate in candidates:
            frequencies[candidate] = frequencies.get(candidate, 0) + (words.count(candidate) if " " not in candidate else 1)
            product_counts[candidate] = product_counts.get(candidate, 0) + 1
    return [
        {"term": term, "occurrences": count, "product_count": product_counts[term]}
        for term, count in sorted(frequencies.items(), key=lambda item: (-item[1], item[0]))[:limit]
    ]


def get_insights(db: Session, business_role: str | None = None) -> dict:
    statement = select(Product).options(joinedload(Product.store), joinedload(Product.reviews)).where(
        Product.status == "active"
    )
    if business_role:
        statement = statement.where(Product.business_role == business_role)
    products = list(db.scalars(statement).unique())
    reviews = [review for product in products for review in product.reviews]
    rating_distribution: dict[str, int] = {str(star): 0 for star in range(1, 6)}
    rating_distribution["unknown"] = 0
    for review in reviews:
        key = str(int(review.rating)) if review.rating is not None and review.rating == int(review.rating) else "unknown"
        if key not in rating_distribution:
            key = "unknown"
        rating_distribution[key] += 1
    tasks = list(
        db.scalars(task_query().order_by(ReviewCollectionTask.created_at.desc()).limit(30))
    )
    return {
        "total_reviews": len(reviews),
        "rating_distribution": rating_distribution,
        "common_terms": ranked_terms(products, "review"),
        "title_terms": ranked_terms(products, "title"),
        "collection_tasks": [serialize_task(task) for task in tasks],
    }
