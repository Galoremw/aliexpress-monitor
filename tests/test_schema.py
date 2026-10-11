from datetime import date
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from app.db.models import (
    BrowserCollectionItem,
    BrowserCollectionRun,
    Product,
    ProductDailyMetric,
    ProductSnapshot,
    Store,
    StoreDailyMetric,
)
from app.core.config import normalize_database_url


def test_all_tables_are_created(db_session):
    tables = set(inspect(db_session.get_bind()).get_table_names())
    assert {
        "stores",
        "products",
        "product_snapshots",
        "product_daily_metrics",
        "store_daily_metrics",
        "store_snapshots",
        "browser_collection_runs",
        "browser_collection_items",
        "product_reviews",
        "review_collection_tasks",
    } <= tables


def test_render_postgres_url_is_normalized_for_psycopg():
    assert normalize_database_url(
        "postgresql://user:pass@host:5432/db"
    ) == "postgresql+psycopg://user:pass@host:5432/db"
    assert normalize_database_url(
        "postgresql+psycopg://user:pass@host:5432/db"
    ) == "postgresql+psycopg://user:pass@host:5432/db"


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
    assert "product_reviews" in migrated_tables
    assert "review_collection_tasks" in migrated_tables
    engine.dispose()
    command.downgrade(config, "20260922_0006")
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    downgraded = inspect(engine)
    assert "product_reviews" not in downgraded.get_table_names()
    assert "review_collection_tasks" not in downgraded.get_table_names()
    assert "business_role" not in {column["name"] for column in downgraded.get_columns("products")}
    engine.dispose()
    get_settings.cache_clear()


def test_browser_control_migration_upgrades_and_downgrades_on_disposable_sqlite(
    tmp_path: Path, monkeypatch
):
    from sqlalchemy import create_engine, text

    database_path = tmp_path / "browser-control-migration.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", database_url)

    from app.core.config import get_settings

    get_settings.cache_clear()
    config = Config("alembic.ini")
    command.upgrade(config, "20260927_0009")
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text(
            """INSERT INTO browser_collection_runs
            (target_date, trigger, status, phase, total_count, succeeded_count,
             partial_count, failed_count, created_at)
            VALUES ('2026-10-09', 'SCHEDULED', 'PENDING', 'STORE_DISCOVERY',
                    1, 0, 0, 0, CURRENT_TIMESTAMP)"""
        ))
        connection.execute(text(
            """INSERT INTO browser_collection_items
            (run_id, target_type, target_key, target_url, position, status, attempt_count)
            VALUES (1, 'STORE', 'store:preserved',
                    'https://www.aliexpress.com/store/123/pages/all-items.html',
                    1, 'PENDING', 0)"""
        ))
    engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(database_url)
    with engine.connect() as connection:
        run = connection.execute(text(
            "SELECT id, dispatch_enabled FROM browser_collection_runs"
        )).one()
        item = connection.execute(text(
            "SELECT id, run_id, target_key, status FROM browser_collection_items"
        )).one()
        tables = set(inspect(connection).get_table_names())
    assert run.id == 1
    assert run.dispatch_enabled in (0, False)
    assert (item.id, item.run_id, item.target_key, item.status) == (
        1, 1, "store:preserved", "PENDING"
    )
    assert {"browser_collection_controls", "browser_collection_workers"} <= tables
    engine.dispose()

    command.downgrade(config, "20260927_0009")
    engine = create_engine(database_url)
    with engine.connect() as connection:
        rows = connection.execute(text(
            "SELECT run_id, target_key, status FROM browser_collection_items"
        )).all()
        tables = set(inspect(connection).get_table_names())
    assert rows == [(1, "store:preserved", "PENDING")]
    assert "browser_collection_controls" not in tables
    assert "browser_collection_workers" not in tables
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


def test_browser_collection_relationships(db_session):
    store = Store(name="Browser Store", url="https://www.aliexpress.com/store/900001")
    product = Product(
        store=store,
        aliexpress_product_id="100500900001",
        url="https://www.aliexpress.com/item/100500900001.html",
    )
    run = BrowserCollectionRun(target_date=date(2026, 9, 21))
    item = BrowserCollectionItem(
        run=run,
        target_type="PRODUCT",
        target_key="product:1",
        store=store,
        product=product,
        target_url=product.url,
        position=1,
    )
    db_session.add(item)
    db_session.commit()

    assert run.items[0].product.aliexpress_product_id == "100500900001"
    assert item.store.name == "Browser Store"
