from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from app.api.utils import canonical_product_url, extract_product_id, normalize_aliexpress_url
from app.db.models import (
    BrowserCollectionWorker,
    DianxiaomiHandoff,
    DianxiaomiQueueControl,
    Product,
    Store,
)


ACTIVE_HANDOFF_STATUSES = {
    "QUEUED",
    "CLAIMED",
    "OPENED",
    "FILLED",
    "COLLECTING",
    "NEEDS_CONFIRMATION",
}
LEASED_HANDOFF_STATUSES = {"CLAIMED", "OPENED", "FILLED", "COLLECTING"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _is_product_page_mismatch(
    error_type: str | None,
    error_message: str | None,
    detail: dict | None = None,
) -> bool:
    if error_type == "product_page_mismatch":
        return True
    evidence = detail.get("product_page_mismatch") if isinstance(detail, dict) else None
    if isinstance(evidence, dict):
        expected_id = str(evidence.get("expected_product_id") or "")
        actual_id = str(evidence.get("actual_product_id") or "")
        if expected_id.isdigit() and actual_id.isdigit() and expected_id != actual_id:
            return True
    match = re.search(
        r"目标商品 ID\s*(\d+)\D+当前商品 ID\s*(\d+)",
        error_message or "",
    )
    return bool(match and match.group(1) != match.group(2))


def _approved_product_pages(db: Session, products: list[Product]) -> dict[int, dict]:
    product_by_id = {product.id: product for product in products}
    product_ids = list(product_by_id)
    if not product_ids:
        return {}
    approved: dict[int, dict] = {}
    history = db.scalars(
        select(DianxiaomiHandoff)
        .where(DianxiaomiHandoff.product_id.in_(product_ids))
        .order_by(DianxiaomiHandoff.requested_at.desc(), DianxiaomiHandoff.id.desc())
    )
    for handoff in history:
        if handoff.product_id in approved or not isinstance(handoff.detail_json, dict):
            continue
        page = handoff.detail_json.get("approved_product_page")
        if not isinstance(page, dict):
            continue
        product = product_by_id[handoff.product_id]
        if str(page.get("monitored_product_id") or "") != product.aliexpress_product_id:
            continue
        try:
            url = normalize_aliexpress_url(str(page.get("url") or ""), "product")
            if extract_product_id(url) != str(page.get("actual_product_id") or ""):
                continue
        except Exception:
            continue
        approved[handoff.product_id] = {**page, "url": url}
    return approved


def _require_product_page_identity_review(row: DianxiaomiHandoff) -> None:
    if row.status != "NEEDS_CONFIRMATION" or not _is_product_page_mismatch(
        row.error_type, row.error_message, row.detail_json
    ):
        raise ValueError("该任务不是待审核的商品页面 ID 不一致任务")


def _product_page_identity_for_review(row: DianxiaomiHandoff) -> tuple[str, str, str]:
    _require_product_page_identity_review(row)
    try:
        target_id = extract_product_id(row.target_url)
    except Exception as exc:
        raise ValueError("队列商品 URL 无法识别商品 ID") from exc
    detail = row.detail_json if isinstance(row.detail_json, dict) else {}
    if not row.product:
        raise ValueError("监控商品记录不存在，不能保存人工授权映射")
    monitored_product_id = row.product.aliexpress_product_id
    target_is_known = target_id == monitored_product_id
    approved_page = detail.get("approved_product_page")
    if not target_is_known and isinstance(approved_page, dict):
        try:
            approved_url = normalize_aliexpress_url(str(approved_page.get("url") or ""), "product")
            target_is_known = (
                str(approved_page.get("monitored_product_id") or "") == monitored_product_id
                and extract_product_id(approved_url) == target_id
            )
        except Exception:
            target_is_known = False
    if not target_is_known:
        raise ValueError("队列 URL 与监控商品及其已审核 URL 均不匹配，不能记录人工授权映射")

    if isinstance(approved_page, dict):
        approved_actual_id = str(approved_page.get("actual_product_id") or "")
        try:
            approved_url = normalize_aliexpress_url(str(approved_page.get("url") or ""), "product")
            if (
                str(approved_page.get("monitored_product_id") or "") == monitored_product_id
                and approved_actual_id.isdigit()
                and extract_product_id(approved_url) == approved_actual_id == target_id
            ):
                # This task already points at a manually approved page. Ignore
                # mismatch evidence left by the earlier review when a later
                # helper error is reviewed again.
                return monitored_product_id, approved_actual_id, approved_url
        except Exception:
            pass

    mismatch = detail.get("product_page_mismatch")
    mismatch = mismatch if isinstance(mismatch, dict) else {}
    expected_id = str(mismatch.get("expected_product_id") or target_id)
    actual_id = str(mismatch.get("actual_product_id") or "")
    actual_url = str(mismatch.get("actual_url") or "")
    if not actual_id:
        match = re.search(
            r"目标商品 ID\s*(\d+)\D+当前商品 ID\s*(\d+)",
            row.error_message or "",
        )
        if match:
            expected_id, actual_id = match.groups()
    if expected_id != target_id:
        raise ValueError("商品 ID 与队列 URL 不匹配，无法确认")
    if not actual_id and actual_url:
        try:
            actual_id = extract_product_id(normalize_aliexpress_url(actual_url, "product"))
        except Exception:
            actual_id = ""
    if not actual_id and isinstance(approved_page, dict):
        # Reuse an approval only when the queued URL itself is the approved
        # page. Never infer that the monitored product URL is the page that
        # actually opened after an AliExpress redirect.
        approved_actual_id = str(approved_page.get("actual_product_id") or "")
        try:
            approved_url = normalize_aliexpress_url(str(approved_page.get("url") or ""), "product")
            if (
                str(approved_page.get("monitored_product_id") or "") == monitored_product_id
                and approved_actual_id.isdigit()
                and extract_product_id(approved_url) == approved_actual_id == target_id
            ):
                actual_id = approved_actual_id
                actual_url = row.target_url
        except Exception:
            pass
    if not actual_id.isdigit():
        raise ValueError("未记录实际商品 ID；请仅对已确认的商品 URL 执行人工确认")

    try:
        # An already approved alternate URL can report the same URL ID as the
        # page that was opened. The user is confirming that known URL, so keep
        # it as the approved page instead of requiring a nonexistent mismatch.
        if actual_id == expected_id and not actual_url:
            actual_url = row.target_url
        actual_url = normalize_aliexpress_url(
            actual_url or canonical_product_url(actual_id), "product"
        )
        if extract_product_id(actual_url) != actual_id:
            raise ValueError("实际商品 URL 与实际商品 ID 不一致")
    except Exception as exc:
        raise ValueError("实际商品 URL 无效，无法记录授权映射") from exc
    return monitored_product_id, actual_id, actual_url


def _queue_control(db: Session, *, lock: bool = False) -> DianxiaomiQueueControl:
    statement = select(DianxiaomiQueueControl).where(DianxiaomiQueueControl.id == 1)
    if lock:
        statement = statement.with_for_update()
    control = db.scalar(statement)
    if control is None:
        control = DianxiaomiQueueControl(id=1)
        db.add(control)
        db.flush()
    return control


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _worker_is_online(worker: BrowserCollectionWorker | None, now: datetime) -> bool:
    heartbeat = _aware(worker.last_heartbeat_at) if worker else None
    return heartbeat is not None and now - heartbeat <= timedelta(minutes=2)


def _default_dianxiaomi_worker(db: Session) -> BrowserCollectionWorker | None:
    workers = list(db.scalars(select(BrowserCollectionWorker)))
    preferred = [
        worker for worker in workers
        if "第二台电脑" in worker.label
        or "第二台电脑" in (worker.display_label_override or "")
    ]
    if not preferred:
        return None
    return max(
        preferred,
        key=lambda worker: (
            _aware(worker.last_heartbeat_at)
            or datetime.min.replace(tzinfo=timezone.utc),
            worker.worker_id,
        ),
    )


def dianxiaomi_worker_options(db: Session, selected_worker_id: str | None) -> list[dict]:
    now = _now()
    workers = list(
        db.scalars(
            select(BrowserCollectionWorker).order_by(
                BrowserCollectionWorker.label,
                BrowserCollectionWorker.worker_id,
            )
        )
    )
    labels: dict[str, int] = {}
    for worker in workers:
        label = worker.display_label_override or worker.label
        labels[label] = labels.get(label, 0) + 1
    return [
        {
            "worker_id": worker.worker_id,
            "label": worker.display_label_override or worker.label,
            "display_label": (
                f"{worker.display_label_override or worker.label} · {worker.worker_id[:8]}"
                if labels[worker.display_label_override or worker.label] > 1
                else worker.display_label_override or worker.label
            ),
            "online": _worker_is_online(worker, now),
            "last_heartbeat_at": worker.last_heartbeat_at,
            "selected": worker.worker_id == selected_worker_id,
        }
        for worker in workers
    ]


def reconcile_dianxiaomi_queue_worker(db: Session) -> DianxiaomiQueueControl:
    control = _queue_control(db, lock=True)
    now = _now()
    if not control.selected_worker_id:
        default_worker = _default_dianxiaomi_worker(db)
        if default_worker is not None:
            control.selected_worker_id = default_worker.worker_id

    worker = (
        db.get(BrowserCollectionWorker, control.selected_worker_id)
        if control.selected_worker_id else None
    )
    queued_work = db.scalar(
        select(DianxiaomiHandoff.id)
        .where(DianxiaomiHandoff.status.in_(LEASED_HANDOFF_STATUSES | {"QUEUED"}))
        .limit(1)
    )
    online = _worker_is_online(worker, now)
    if queued_work is not None and not online and not control.is_paused:
        control.is_paused = True
        control.paused_at = now
        control.paused_reason = "selected_worker_offline"
    elif online and control.is_paused and control.paused_reason == "selected_worker_offline":
        control.is_paused = False
        control.paused_at = None
        control.paused_reason = None
    db.commit()
    db.refresh(control)
    return control


def select_dianxiaomi_worker(db: Session, worker_id: str) -> DianxiaomiQueueControl:
    control = _queue_control(db, lock=True)
    worker = db.get(BrowserCollectionWorker, worker_id)
    if worker is None:
        raise ValueError("所选采集电脑尚未连接监控台")
    control.selected_worker_id = worker.worker_id
    db.commit()
    return reconcile_dianxiaomi_queue_worker(db)


def _prepare_handoff_claim(
    db: Session, worker_id: str, *, resume_confirmed: bool = False
) -> tuple[DianxiaomiQueueControl, datetime] | None:
    now = _now()
    control = _queue_control(db, lock=True)
    if not control.selected_worker_id:
        default_worker = _default_dianxiaomi_worker(db)
        if default_worker is not None:
            control.selected_worker_id = default_worker.worker_id
    selected_worker_id = control.selected_worker_id
    selected_worker = (
        db.get(BrowserCollectionWorker, selected_worker_id)
        if selected_worker_id else None
    )
    selected_online = _worker_is_online(selected_worker, now)
    worker = db.get(BrowserCollectionWorker, worker_id)
    if not selected_worker_id or not selected_online:
        queued_work = db.scalar(
            select(DianxiaomiHandoff.id)
            .where(DianxiaomiHandoff.status == "QUEUED")
            .limit(1)
        )
        if queued_work is not None and not control.is_paused:
            control.is_paused = True
            control.paused_at = now
            control.paused_reason = "selected_worker_offline"
        db.commit()
        return None
    if selected_worker_id != worker_id or not _worker_is_online(worker, now):
        db.commit()
        return None

    if control.is_paused and control.paused_reason == "selected_worker_offline":
        control.is_paused = False
        control.paused_at = None
        control.paused_reason = None
    if control.is_paused and not (
        resume_confirmed and control.paused_reason == "needs_confirmation"
    ):
        return None

    if resume_confirmed:
        paused = list(
            db.scalars(
                select(DianxiaomiHandoff).where(
                    DianxiaomiHandoff.status == "NEEDS_CONFIRMATION",
                    DianxiaomiHandoff.worker_id == worker_id,
                )
            )
        )
        for row in paused:
            row.status = "QUEUED"
            row.worker_id = None
            row.claimed_at = None
            row.error_type = None
            row.error_message = None
            row.completed_at = None
        if paused:
            db.flush()
        if control.paused_reason == "needs_confirmation":
            control.is_paused = False
            control.paused_at = None
            control.paused_reason = None

    expired = list(
        db.scalars(
            select(DianxiaomiHandoff).where(
                DianxiaomiHandoff.status.in_(LEASED_HANDOFF_STATUSES),
                or_(
                    DianxiaomiHandoff.claimed_at.is_(None),
                    DianxiaomiHandoff.claimed_at < now - timedelta(minutes=10),
                ),
            )
        )
    )
    for row in expired:
        row.status = "QUEUED"
        row.worker_id = None
        row.claimed_at = None
        row.error_type = None
        row.error_message = None
    if expired:
        db.flush()

    other_active = db.scalar(
        select(DianxiaomiHandoff.id)
        .where(
            DianxiaomiHandoff.status.in_(LEASED_HANDOFF_STATUSES),
            DianxiaomiHandoff.worker_id != worker_id,
            or_(
                DianxiaomiHandoff.claimed_at.is_(None),
                DianxiaomiHandoff.claimed_at >= now - timedelta(minutes=10),
            ),
        )
        .limit(1)
    )
    if other_active is not None:
        return None
    return control, now


def pause_dianxiaomi_queue(
    db: Session, reason: str = "manual_pause"
) -> DianxiaomiQueueControl:
    control = _queue_control(db)
    control.is_paused = True
    control.paused_at = _now()
    control.paused_reason = reason
    db.commit()
    db.refresh(control)
    return control


def resume_dianxiaomi_queue(db: Session) -> DianxiaomiQueueControl:
    control = _queue_control(db, lock=True)
    if not control.selected_worker_id:
        default_worker = _default_dianxiaomi_worker(db)
        if default_worker is not None:
            control.selected_worker_id = default_worker.worker_id
    worker = (
        db.get(BrowserCollectionWorker, control.selected_worker_id)
        if control.selected_worker_id else None
    )
    if not _worker_is_online(worker, _now()):
        raise ValueError("店小秘指定采集电脑离线，队列会在它重新在线后自动继续")
    control.is_paused = False
    control.paused_at = None
    control.paused_reason = None
    db.commit()
    db.refresh(control)
    return control


def record_dianxiaomi_heartbeat(
    db: Session, worker_id: str
) -> DianxiaomiQueueControl:
    control = _queue_control(db, lock=True)
    now = _now()
    if not control.selected_worker_id:
        default_worker = _default_dianxiaomi_worker(db)
        if default_worker is not None:
            control.selected_worker_id = default_worker.worker_id
    selected_worker_id = control.selected_worker_id
    if selected_worker_id == worker_id:
        control.last_heartbeat_at = now
        control.worker_id = worker_id
    # A queue poll also proves that its registered Chrome profile is alive.
    # The dedicated queue target is only defaulted when it has not been set.
    browser_worker = db.get(BrowserCollectionWorker, worker_id)
    if browser_worker is not None:
        browser_worker.last_heartbeat_at = now
    # Worker liveness must not renew every handoff assigned to this extension.
    # A restarted worker can otherwise keep legacy OPENED rows leased forever.
    db.commit()
    if selected_worker_id == worker_id:
        return reconcile_dianxiaomi_queue_worker(db)
    db.refresh(control)
    return control


def create_handoff_batch(db: Session, product_ids: list[int]) -> tuple[str, list[DianxiaomiHandoff]]:
    unique_ids = list(dict.fromkeys(product_ids))
    products = list(
        db.scalars(
            select(Product)
            .join(Store, Store.id == Product.store_id)
            .where(
                Product.id.in_(unique_ids),
                Product.status == "active",
                Store.status == "active",
            )
        )
    )
    by_id = {product.id: product for product in products}
    missing = [product_id for product_id in unique_ids if product_id not in by_id]
    if missing:
        raise ValueError(f"存在不可用的监控商品: {', '.join(map(str, missing))}")

    approved_pages = _approved_product_pages(db, products)
    batch_id = uuid4().hex
    existing = {
        handoff.product_id
        for handoff in db.scalars(
            select(DianxiaomiHandoff).where(
                DianxiaomiHandoff.product_id.in_(unique_ids),
                DianxiaomiHandoff.status.in_(ACTIVE_HANDOFF_STATUSES),
            )
        )
    }
    rows: list[DianxiaomiHandoff] = []
    for product_id in unique_ids:
        if product_id in existing:
            continue
        product = by_id[product_id]
        approved_page = approved_pages.get(product.id)
        rows.append(
            DianxiaomiHandoff(
                batch_id=batch_id,
                product_id=product.id,
                store_id=product.store_id,
                target_url=approved_page["url"] if approved_page else product.url,
                status="QUEUED",
                detail_json={"approved_product_page": approved_page} if approved_page else None,
            )
        )
    db.add_all(rows)
    db.commit()
    for row in rows:
        db.refresh(row)
    return batch_id, rows


def claim_next_handoff(db: Session, worker_id: str) -> DianxiaomiHandoff | None:
    rows = claim_handoff_batch(db, worker_id, limit=1)
    return rows[0] if rows else None


def claim_handoff_batch(
    db: Session, worker_id: str, *, limit: int = 20, resume_confirmed: bool = False
) -> list[DianxiaomiHandoff]:
    prepared = _prepare_handoff_claim(
        db, worker_id, resume_confirmed=resume_confirmed
    )
    if prepared is None:
        db.commit()
        return []
    control, now = prepared

    rows = list(
        db.scalars(
            select(DianxiaomiHandoff)
            .where(DianxiaomiHandoff.status == "QUEUED")
            .order_by(DianxiaomiHandoff.requested_at, DianxiaomiHandoff.id)
            .limit(limit)
        )
    )
    if not rows:
        db.commit()
        return []
    for row in rows:
        row.status = "CLAIMED"
        row.claimed_at = now
        row.worker_id = worker_id
    control.last_heartbeat_at = now
    control.worker_id = worker_id
    db.commit()
    for row in rows:
        db.refresh(row)
    return rows


def claim_handoff(
    db: Session, handoff_id: int, worker_id: str
) -> DianxiaomiHandoff | None:
    prepared = _prepare_handoff_claim(db, worker_id)
    if prepared is None:
        db.commit()
        return None
    control, now = prepared
    row = db.get(DianxiaomiHandoff, handoff_id)
    if row is None or row.status != "QUEUED":
        db.commit()
        return None
    row.status = "CLAIMED"
    row.claimed_at = now
    row.worker_id = worker_id
    control.last_heartbeat_at = now
    control.worker_id = worker_id
    db.commit()
    db.refresh(row)
    return row


def update_handoff(
    db: Session,
    row: DianxiaomiHandoff,
    status: str,
    *,
    error_type: str | None = None,
    error_message: str | None = None,
    detail: dict | None = None,
    worker_id: str | None = None,
) -> DianxiaomiHandoff:
    now = _now()
    if status == "NEEDS_CONFIRMATION" and _is_product_page_mismatch(
        error_type, error_message, detail
    ):
        # Older collector builds sent the mismatch text with the generic
        # manual_confirmation_required type. Normalize it so the queue keeps
        # moving and the dashboard offers the identity decision buttons.
        error_type = "product_page_mismatch"
    row.status = status
    row.error_type = error_type
    row.error_message = error_message
    if detail is not None:
        previous_detail = row.detail_json if isinstance(row.detail_json, dict) else {}
        merged_detail = dict(detail)
        previous_approval = previous_detail.get("approved_product_page")
        if "approved_product_page" not in merged_detail and isinstance(previous_approval, dict):
            merged_detail["approved_product_page"] = previous_approval
        row.detail_json = merged_detail
    if worker_id:
        row.worker_id = worker_id
    if status in LEASED_HANDOFF_STATUSES:
        row.claimed_at = now
    if status == "NEEDS_CONFIRMATION" and error_type != "product_page_mismatch":
        control = _queue_control(db)
        if not control.is_paused:
            control.is_paused = True
            control.paused_at = now
            control.paused_reason = "needs_confirmation"
    if status == "OPENED":
        row.opened_at = row.opened_at or now
    if status in {"SUCCEEDED", "FAILED", "CANCELED"}:
        row.completed_at = now
    db.commit()
    db.refresh(row)
    return row


def retry_handoff(db: Session, row: DianxiaomiHandoff) -> DianxiaomiHandoff:
    if row.status not in {"NEEDS_CONFIRMATION", "FAILED"}:
        raise ValueError("该店小秘任务当前不能重新排队")
    if _is_product_page_mismatch(row.error_type, row.error_message, row.detail_json):
        raise ValueError("该任务需要先确认商品页面身份；请使用“确认商品无误”或“错误”处理")
    row.status = "QUEUED"
    row.worker_id = None
    row.claimed_at = None
    row.error_type = None
    row.error_message = None
    detail = row.detail_json if isinstance(row.detail_json, dict) else {}
    approved_page = detail.get("approved_product_page")
    row.detail_json = {"approved_product_page": approved_page} if approved_page else None
    row.completed_at = None
    row.attempt_count += 1
    db.commit()
    db.refresh(row)
    return row


def confirm_product_page_identity(
    db: Session, row: DianxiaomiHandoff
) -> DianxiaomiHandoff:
    expected_id, actual_id, actual_url = _product_page_identity_for_review(row)
    detail = dict(row.detail_json) if isinstance(row.detail_json, dict) else {}
    detail.pop("product_page_mismatch", None)
    detail["approved_product_page"] = {
        "monitored_product_id": expected_id,
        "actual_product_id": actual_id,
        "url": actual_url,
        "source_handoff_id": row.id,
        "approved_at": _now().isoformat(),
    }
    row.target_url = actual_url
    row.detail_json = detail
    row.status = "QUEUED"
    row.worker_id = None
    row.claimed_at = None
    row.opened_at = None
    row.completed_at = None
    row.error_type = None
    row.error_message = None
    row.attempt_count += 1
    row.requested_at = _now()
    db.commit()
    db.refresh(row)
    return row


def reject_product_page_identity(
    db: Session, row: DianxiaomiHandoff
) -> DianxiaomiHandoff:
    _require_product_page_identity_review(row)
    row.status = "CANCELED"
    row.completed_at = _now()
    row.error_type = "product_page_identity_rejected"
    row.error_message = "人工判断实际商品页与监控商品不一致"
    db.commit()
    db.refresh(row)
    return row


def delete_failed_handoffs(db: Session) -> int:
    result = db.execute(
        delete(DianxiaomiHandoff).where(DianxiaomiHandoff.status == "FAILED")
    )
    db.commit()
    return max(int(result.rowcount or 0), 0)


def serialize_handoff(row: DianxiaomiHandoff) -> dict:
    product = row.product
    return {
        "id": row.id,
        "batch_id": row.batch_id,
        "product_id": row.product_id,
        "store_id": row.store_id,
        "product_title": product.title if product else None,
        "platform_product_id": product.aliexpress_product_id if product else None,
        "store_name": row.store.name if row.store else None,
        "target_url": row.target_url,
        "status": row.status,
        "requested_at": row.requested_at,
        "claimed_at": row.claimed_at,
        "opened_at": row.opened_at,
        "completed_at": row.completed_at,
        "worker_id": row.worker_id,
        "error_type": row.error_type,
        "error_message": row.error_message,
        "attempt_count": row.attempt_count,
        "detail": row.detail_json,
    }
