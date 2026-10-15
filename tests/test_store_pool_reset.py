from datetime import date

import pytest
from pydantic import ValidationError

from app.api import schemas
from app.db import models
from app.db.models import Store


def test_reset_batch_and_store_checkpoint_persist_frozen_window_and_progress(db_session):
    assert hasattr(models, "StorePoolResetBatch")
    assert hasattr(models, "StorePoolResetStore")
    batch_model = models.StorePoolResetBatch
    task_model = models.StorePoolResetStore

    store = Store(
        aliexpress_store_id="reset-store-1",
        name="Reset Store",
        url="https://www.aliexpress.com/store/1100000001",
        status="active",
    )
    batch = batch_model(
        target_date=date(2026, 10, 16),
        window_start=date(2026, 10, 9),
        window_end=date(2026, 10, 15),
        status="PENDING",
        dispatch_enabled=False,
    )
    task = task_model(
        batch=batch,
        store=store,
        platform_store_id="1100000001",
        store_name="Reset Store",
        orders_url="https://www.aliexpress.com/store/1100000001/pages/all-items.html?shop_sortType=orders_desc",
        position=1,
        status="PENDING",
        visited_page_urls=["https://www.aliexpress.com/store/1100000001/pages/all-items.html?shop_sortType=orders_desc"],
        staged_candidates=[{"platform_product_id": "10050001", "orders_rank": 1}],
        diagnostics=[{"reason": "chart_missing", "product_id": "10050002"}],
        scanned_candidate_count=1,
        scanned_page_count=1,
    )
    db_session.add(task)
    db_session.commit()

    loaded = db_session.get(batch_model, batch.id)
    loaded_task = db_session.get(task_model, task.id)
    assert (loaded.target_date, loaded.window_start, loaded.window_end) == (
        date(2026, 10, 16),
        date(2026, 10, 9),
        date(2026, 10, 15),
    )
    assert loaded.status == "PENDING"
    assert loaded.dispatch_enabled is False
    assert loaded_task.staged_candidates == [{"platform_product_id": "10050001", "orders_rank": 1}]
    assert loaded_task.diagnostics == [{"reason": "chart_missing", "product_id": "10050002"}]
    assert loaded_task.visited_page_urls == [task.orders_url]


def test_reset_create_schema_requires_explicit_replace_confirmation():
    request = schemas.StorePoolResetCreateRequest
    with pytest.raises(ValidationError):
        request.model_validate({"confirm_replace": False})

    confirmed = request.model_validate({"confirm_replace": True})
    assert confirmed.confirm_replace is True


def test_legacy_browser_worker_capabilities_default_to_empty(db_session):
    from app.db.models import BrowserCollectionWorker

    assert "capabilities" in BrowserCollectionWorker.__table__.columns
    worker = BrowserCollectionWorker(
        worker_id="old-extension",
        label="Old extension",
        extension_version="0.6.80",
        last_heartbeat_at=models.utcnow(),
    )
    db_session.add(worker)
    db_session.commit()
    db_session.refresh(worker)

    assert worker.capabilities == []

