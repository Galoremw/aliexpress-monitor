import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.models import PricingRecord, PricingRuleVersion
from scripts.import_pricing_data import import_pricing_data


def make_database(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'pricing-import.db'}")
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def sample_payload():
    versions = [
        {
            "version": f"legacy-rule-{index}",
            "source_sha256": f"{index:064x}",
            "source_sheet": f"Sheet {index}",
            "rule_json": {"version": f"legacy-rule-{index}", "example": index},
            "created_at": "2026-10-18T00:00:00+00:00",
        }
        for index in range(1, 4)
    ]
    records = [
        {
            "record_id": f"legacy-record-{index}",
            "product_id": f"product-{index}",
            "product_name": f"Legacy Product {index}",
            "sku": f"SKU-{index}",
            "rule_version": f"legacy-rule-{index}",
            "input_json": {"purchase_price": "10.00"},
            "result_json": {"formula_price": "23.94"},
            "final_price": "25.00" if index == 1 else None,
            "status": "DRAFT" if index == 1 else "CONFIRMED",
            "created_at": "2026-10-18T01:00:00+00:00",
            "confirmed_at": None if index == 1 else "2026-10-18T02:00:00+00:00",
        }
        for index in range(1, 3)
    ]
    return {"pricing_rule_versions": versions, "pricing_records": records}


def write_payload(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "pricing-export.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_import_preserves_three_rule_versions_and_two_records(tmp_path):
    engine, factory = make_database(tmp_path)
    source = write_payload(tmp_path, sample_payload())

    summary = import_pricing_data(source, factory)

    with factory() as db:
        assert db.scalar(select(func.count()).select_from(PricingRuleVersion)) == 3
        assert db.scalar(select(func.count()).select_from(PricingRecord)) == 2
        records = list(db.scalars(select(PricingRecord).order_by(PricingRecord.record_id)))
        assert [record.rule_version for record in records] == ["legacy-rule-1", "legacy-rule-2"]
        assert records[0].input_json == {"purchase_price": "10.00"}
        assert records[0].final_price == 25
        assert records[1].final_price is None
    assert summary.rule_versions_imported == 3
    assert summary.pricing_records_imported == 2
    engine.dispose()


def test_import_is_idempotent_and_preserves_rule_links(tmp_path):
    engine, factory = make_database(tmp_path)
    source = write_payload(tmp_path, sample_payload())

    first = import_pricing_data(source, factory)
    second = import_pricing_data(source, factory)

    assert first.rule_versions_imported == 3
    assert first.pricing_records_imported == 2
    assert second.rule_versions_imported == 0
    assert second.pricing_records_imported == 0
    with factory() as db:
        record = db.get(PricingRecord, "legacy-record-1")
        assert record.rule.version == "legacy-rule-1"
    engine.dispose()


def test_import_rejects_record_with_missing_rule(tmp_path):
    engine, factory = make_database(tmp_path)
    payload = sample_payload()
    payload["pricing_records"][0]["rule_version"] = "not-exported"
    source = write_payload(tmp_path, payload)

    with pytest.raises(ValueError, match="missing pricing rule version"):
        import_pricing_data(source, factory)

    with factory() as db:
        assert db.scalar(select(func.count()).select_from(PricingRuleVersion)) == 0
        assert db.scalar(select(func.count()).select_from(PricingRecord)) == 0
    engine.dispose()
