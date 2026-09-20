from datetime import date
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from app.db.models import Product, ProductDailyMetric, ProductSnapshot, Store, StoreDailyMetric


def test_all_tables_are_created(db_session):
    tables = set(inspect(db_session.get_bind()).get_table_names())
    assert {
        "stores",
        "products",
        "product_snapshots",
        "product_daily_metrics",
        "store_daily_metrics",
        "store_snapshots",
    } <= tables


def test_alembic_upgrade_creates_schema(tmp_path: Path, monkeypatch):
    database_path = tmp_path / "migration.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database_path.as_posix()}")

    from app.core.config import get_settings

    get_settings.cache_clear()
    config = Config("alembic.ini")
    command.upgrade(config, "head")

    from sqlalchemy import create_engine

    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    migrated_tables = inspect(engine).get_table_names()
    assert "product_snapshots" in migrated_tables
    assert "store_snapshots" in migrated_tables
    engine.dispose()
    get_settings.cache_clear()


def test_model_relationships(db_session):
    store = Store(name="Peer Store", url="https://example.aliexpress.com/store/1")
    product = Product(
        store=store,
        aliexpress_product_id="1005001",
        url="https://www.aliexpress.com/item/1005001.html",
    )
    snapshot = ProductSnapshot(
        product=product,
        collector_name="fixture",
        collector_version="1",
        parse_status="success",
        cumulative_sold=10,
        raw_payload={"sold": 10},
    )
    product_metric = ProductDailyMetric(
        product=product,
        metric_date=date(2026, 9, 20),
        estimated_sales=3,
        is_estimable=True,
        reason="ok",
        start_snapshot_id=None,
        end_snapshot_id=None,
    )
    store_metric = StoreDailyMetric(
        store=store,
        metric_date=date(2026, 9, 20),
        estimated_sales=3,
        estimable_products=1,
        unavailable_products=0,
        product_scope_count=1,
    )
    db_session.add_all([snapshot, product_metric, store_metric])
    db_session.commit()

    assert store.products == [product]
    assert product.snapshots[0].raw_payload == {"sold": 10}
    assert store.daily_metrics[0].calculation_method == "sum_of_monitored_product_estimates"
