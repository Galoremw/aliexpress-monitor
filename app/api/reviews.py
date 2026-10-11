import csv
import io
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.api.schemas import (
    ProductReviewRead,
    ReviewCollectionChallenge,
    ReviewCollectionClaimRead,
    ReviewCollectionCreate,
    ReviewCollectionResult,
    ReviewCollectionTaskRead,
    ReviewInsightsRead,
)
from app.core.auth import require_authenticated
from app.db.models import Product, ProductReview, ReviewCollectionTask
from app.db.session import get_db
from app.services.reviews import (
    claim_next_task,
    complete_task,
    create_or_reuse_task,
    get_insights,
    serialize_task,
    task_query,
)

router = APIRouter(prefix="/api", dependencies=[Depends(require_authenticated)])


def _task_or_404(db: Session, task_id: int) -> ReviewCollectionTask:
    task = db.scalar(task_query().where(ReviewCollectionTask.id == task_id))
    if task is None:
        raise HTTPException(status_code=404, detail="Review collection task not found")
    return task


@router.post(
    "/products/{product_id}/reviews/collection-tasks",
    response_model=ReviewCollectionTaskRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_review_collection_task(
    product_id: int,
    payload: ReviewCollectionCreate,
    db: Session = Depends(get_db),
):
    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    if product.status != "active":
        raise HTTPException(status_code=409, detail="商品监控已停用")
    try:
        task = create_or_reuse_task(db, product, payload.max_new_reviews)
    except IntegrityError:
        db.rollback()
        task = db.scalar(
            task_query()
            .where(
                ReviewCollectionTask.product_id == product_id,
                ReviewCollectionTask.status.in_(
                    ("PENDING", "RUNNING", "NEEDS_VERIFICATION")
                ),
            )
            .order_by(ReviewCollectionTask.created_at.desc())
        )
        if task is None:
            raise
    return serialize_task(task)


@router.get("/reviews/collection-tasks", response_model=list[ReviewCollectionTaskRead])
def list_review_collection_tasks(
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    tasks = db.scalars(task_query().order_by(ReviewCollectionTask.created_at.desc()).limit(limit))
    return [serialize_task(task) for task in tasks]


@router.post("/reviews/collection-tasks/claim", response_model=ReviewCollectionClaimRead)
def claim_review_collection_task(
    worker_id: str = Query(default="chrome-extension", max_length=128),
    db: Session = Depends(get_db),
):
    task, known_keys = claim_next_task(db, worker_id)
    return {"task": serialize_task(task) if task else None, "known_keys": known_keys}


@router.post(
    "/reviews/collection-tasks/{task_id}/result",
    response_model=ReviewCollectionTaskRead,
)
def submit_review_collection_result(
    task_id: int,
    payload: ReviewCollectionResult,
    db: Session = Depends(get_db),
):
    task = _task_or_404(db, task_id)
    if task.status != "RUNNING":
        raise HTTPException(status_code=409, detail=f"任务当前状态为 {task.status}")
    if len(payload.reviews) > task.max_new_reviews:
        raise HTTPException(status_code=422, detail="评论数量超过任务上限")
    return serialize_task(complete_task(db, task, payload))


@router.post(
    "/reviews/collection-tasks/{task_id}/challenge",
    response_model=ReviewCollectionTaskRead,
)
def pause_review_collection_for_challenge(
    task_id: int,
    payload: ReviewCollectionChallenge,
    db: Session = Depends(get_db),
):
    task = _task_or_404(db, task_id)
    if task.status != "RUNNING":
        raise HTTPException(status_code=409, detail=f"任务当前状态为 {task.status}")
    task.status = "NEEDS_VERIFICATION"
    task.lease_expires_at = None
    task.error_type = "platform_challenge"
    task.error_message = payload.error_message or "AliExpress 要求人工完成验证"
    db.commit()
    db.refresh(task)
    return serialize_task(task)


@router.post(
    "/reviews/collection-tasks/{task_id}/resume",
    response_model=ReviewCollectionTaskRead,
)
def resume_review_collection_after_challenge(
    task_id: int, db: Session = Depends(get_db)
):
    task = _task_or_404(db, task_id)
    if task.status != "NEEDS_VERIFICATION":
        raise HTTPException(status_code=409, detail=f"任务当前状态为 {task.status}")
    task.status = "RUNNING"
    task.claimed_at = task.claimed_at or task.created_at
    task.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=15)
    task.error_type = None
    task.error_message = None
    db.commit()
    db.refresh(task)
    return serialize_task(task)


@router.post(
    "/reviews/collection-tasks/{task_id}/retry",
    response_model=ReviewCollectionTaskRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def retry_review_collection_task(task_id: int, db: Session = Depends(get_db)):
    task = _task_or_404(db, task_id)
    if task.status not in ("FAILED", "PARTIAL", "SUCCEEDED"):
        raise HTTPException(status_code=409, detail="当前任务仍在进行或等待验证")
    product = db.get(Product, task.product_id)
    if product is None or product.status != "active":
        raise HTTPException(status_code=409, detail="商品监控已停用")
    new_task = create_or_reuse_task(db, product, task.max_new_reviews)
    return serialize_task(new_task)


@router.get("/products/{product_id}/reviews", response_model=list[ProductReviewRead])
def list_product_reviews(
    product_id: int,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    if db.get(Product, product_id) is None:
        raise HTTPException(status_code=404, detail="Product not found")
    return list(
        db.scalars(
            select(ProductReview)
            .where(ProductReview.product_id == product_id)
            .order_by(ProductReview.review_date.desc().nullslast(), ProductReview.id.desc())
            .offset(offset)
            .limit(limit)
        )
    )


@router.get("/reviews/insights", response_model=ReviewInsightsRead)
def review_insights(
    business_role: str | None = Query(default=None, pattern="^(SELF|COMPETITOR|UNCLASSIFIED)$"),
    db: Session = Depends(get_db),
):
    return get_insights(db, business_role)


@router.get("/reviews/export")
def export_reviews(
    format: str = Query(default="csv", pattern="^(csv|markdown)$"),
    business_role: str | None = Query(default=None, pattern="^(SELF|COMPETITOR|UNCLASSIFIED)$"),
    product_id: int | None = Query(default=None, ge=1),
    db: Session = Depends(get_db),
):
    statement = (
        select(ProductReview)
        .join(Product, Product.id == ProductReview.product_id)
        .options(joinedload(ProductReview.product).joinedload(Product.store))
        .order_by(ProductReview.review_date.desc().nullslast(), ProductReview.id.desc())
    )
    if business_role:
        statement = statement.where(Product.business_role == business_role)
    if product_id is not None:
        statement = statement.where(Product.id == product_id)
    reviews = list(db.scalars(statement.limit(10000)))
    filename = "aliexpress-public-reviews"
    if format == "csv":
        stream = io.StringIO(newline="")
        writer = csv.writer(stream)
        writer.writerow(
            ["product_id", "product_title", "store", "business_role", "review_text", "rating", "date", "country_region", "variant", "has_media", "verified_purchase"]
        )
        for review in reviews:
            product = review.product
            writer.writerow(
                [product.id, product.title or "", product.store.name, product.business_role,
                 review.review_text, review.rating, review.review_date, review.country_region or "",
                 review.variant or "", review.has_media, review.verified_purchase]
            )
        return Response(
            content="\ufeff" + stream.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}.csv"'},
        )

    lines = ["# AliExpress 公开评论与商品标题词材料", "", "> 评论来自页面公开可见信息；不包含评论者昵称或个人主页。标题词频仅代表当前监控商品标题中的出现频率，不代表平台搜索量。", "", "## 评论样本"]
    for review in reviews:
        product = review.product
        details = [product.title or product.aliexpress_product_id, review.rating or "评分未知"]
        if review.review_date:
            details.append(review.review_date.isoformat())
        if review.country_region:
            details.append(review.country_region)
        lines.extend([f"### {' · '.join(map(str, details))}", "", review.review_text, ""])
    lines.extend(
        [
            "## 给 ChatGPT 的中文分析提示词",
            "请仅根据上方评论样本分析，并区分证据与推断。总结：1. 购买动机；2. 正面反馈；3. 负面反馈及反复出现的问题；4. 未满足需求；5. 可执行的产品改进方向；6. 适合标题测试的高频词。不要把标题词频称为真实搜索量，也不要把所有评论称为已验证购买。对样本数量和偏差保持谨慎。",
        ]
    )
    return Response(
        content="\n".join(lines),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}.md"'},
    )
