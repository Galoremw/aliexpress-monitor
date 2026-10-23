"""Import an explicit pricing-only JSON export into the monitor database."""

import argparse
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from app.db.models import PricingRecord, PricingRuleVersion


RULE_FIELDS = {"version", "source_sha256", "source_sheet", "rule_json", "created_at"}
RECORD_FIELDS = {
    "record_id",
    "product_id",
    "product_name",
    "sku",
    "rule_version",
    "input_json",
    "result_json",
    "final_price",
    "status",
    "created_at",
    "confirmed_at",
}
EXPORT_TABLES = {"pricing_rule_versions", "pricing_records"}


@dataclass(frozen=True)
class ImportSummary:
    rule_versions_imported: int
    pricing_records_imported: int


def import_pricing_data(source_json: Path, session_factory: sessionmaker[Session]) -> ImportSummary:
    payload = json.loads(source_json.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or set(payload) != EXPORT_TABLES:
        raise ValueError("export must contain only pricing_rule_versions and pricing_records")
    rules = payload["pricing_rule_versions"]
    records = payload["pricing_records"]
    if not isinstance(rules, list) or not isinstance(records, list):
        raise ValueError("pricing export tables must be arrays")
    _validate_rows(rules, RULE_FIELDS, "pricing_rule_versions")
    _validate_rows(records, RECORD_FIELDS, "pricing_records")

    imported_rules = 0
    imported_records = 0
    with session_factory() as db:
        try:
            for row in rules:
                version = row["version"]
                existing = db.get(PricingRuleVersion, version)
                if existing is not None:
                    if not _same_rule(existing, row):
                        raise ValueError(f"pricing rule version conflicts with existing data: {version}")
                    continue
                db.add(
                    PricingRuleVersion(
                        version=version,
                        source_sha256=row["source_sha256"],
                        source_sheet=row["source_sheet"],
                        rule_json=row["rule_json"],
                        created_at=_parse_datetime(row["created_at"]),
                    )
                )
                imported_rules += 1
            db.flush()

            for row in records:
                record_id = row["record_id"]
                existing = db.get(PricingRecord, record_id)
                if existing is not None:
                    if not _same_record(existing, row):
                        raise ValueError(f"pricing record conflicts with existing data: {record_id}")
                    continue
                if db.get(PricingRuleVersion, row["rule_version"]) is None:
                    raise ValueError(
                        f"record references missing pricing rule version: {record_id}"
                    )
                db.add(
                    PricingRecord(
                        record_id=record_id,
                        product_id=row["product_id"],
                        product_name=row["product_name"],
                        sku=row["sku"],
                        rule_version=row["rule_version"],
                        input_json=row["input_json"],
                        result_json=row["result_json"],
                        final_price=(
                            Decimal(str(row["final_price"]))
                            if row["final_price"] is not None
                            else None
                        ),
                        status=row["status"],
                        created_at=_parse_datetime(row["created_at"]),
                        confirmed_at=_parse_datetime(row["confirmed_at"]),
                    )
                )
                imported_records += 1
            db.commit()
        except Exception:
            db.rollback()
            raise
    return ImportSummary(imported_rules, imported_records)


def _validate_rows(rows: list[Any], allowed_fields: set[str], table_name: str) -> None:
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) != allowed_fields:
            raise ValueError(f"{table_name}[{index}] does not match the approved column whitelist")
    if table_name == "pricing_rule_versions":
        if any(not isinstance(row["rule_json"], dict) for row in rows):
            raise ValueError("pricing rule JSON must be an object")
    else:
        if any(not isinstance(row["input_json"], dict) or not isinstance(row["result_json"], dict) for row in rows):
            raise ValueError("pricing record input and result JSON must be objects")


def _parse_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return parsed


def _same_rule(existing: PricingRuleVersion, row: dict[str, Any]) -> bool:
    return (
        existing.source_sha256 == row["source_sha256"]
        and existing.source_sheet == row["source_sheet"]
        and existing.rule_json == row["rule_json"]
    )


def _same_record(existing: PricingRecord, row: dict[str, Any]) -> bool:
    return (
        existing.product_id == row["product_id"]
        and existing.product_name == row["product_name"]
        and existing.sku == row["sku"]
        and existing.rule_version == row["rule_version"]
        and existing.input_json == row["input_json"]
        and existing.result_json == row["result_json"]
        and existing.final_price
        == (Decimal(str(row["final_price"])) if row["final_price"] is not None else None)
        and existing.status == row["status"]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Import a pricing-only JSON export")
    parser.add_argument("source_json", type=Path, help="path to the approved pricing export")
    args = parser.parse_args()
    from app.db.session import get_session_factory

    try:
        summary = import_pricing_data(args.source_json, get_session_factory())
    except Exception as exc:
        print(f"Pricing import failed: {exc}")
        return 1
    print(
        "Imported pricing rule versions: "
        f"{summary.rule_versions_imported}; pricing records: {summary.pricing_records_imported}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
