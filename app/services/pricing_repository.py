"""Persistence for immutable pricing rules and user-confirmed price records."""

import json
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from app.api.pricing_schemas import PricingQuoteInput
from app.db.models import PricingRecord, PricingRuleVersion
from app.services.pricing import ORDINARY_RULE_PATH, PRINTED_3D_RULE_PATH


class PricingRepository:
    def __init__(
        self,
        rule_paths: tuple[Path, ...] = (ORDINARY_RULE_PATH, PRINTED_3D_RULE_PATH),
    ) -> None:
        self.rule_paths = rule_paths

    def ensure_rule_versions(self, db: Session) -> None:
        added = False
        for path in self.rule_paths:
            rule = json.loads(path.read_text(encoding="utf-8"))
            profiles = [
                (rule["version"], rule),
                (
                    rule["workbook_profile"]["version"],
                    {
                        **rule["workbook_profile"],
                        "parent_version": rule["version"],
                        "source_formulas": rule.get("source_formulas", {}),
                    },
                ),
            ]
            for version, rule_json in profiles:
                if db.get(PricingRuleVersion, version) is not None:
                    continue
                db.add(
                    PricingRuleVersion(
                        version=version,
                        source_sha256=rule["source_sha256"],
                        source_sheet=rule["sheet"],
                        rule_json=rule_json,
                    )
                )
                added = True
        if added:
            db.commit()

    def save(
        self,
        db: Session,
        payload: PricingQuoteInput,
        quote: dict[str, Any],
    ) -> PricingRecord:
        if payload.final_price is None:
            raise ValueError("a manual final price is required before saving")
        version = quote.get("rule_version")
        if not version or db.get(PricingRuleVersion, version) is None:
            raise ValueError("unknown pricing rule version")
        record = PricingRecord(
            record_id=uuid4().hex,
            product_id=payload.product_id,
            product_name=payload.product_name or None,
            sku=payload.sku or None,
            rule_version=version,
            input_json=self._json_value(payload.model_dump(mode="python")),
            result_json=self._json_value(quote),
            final_price=payload.final_price,
            status="DRAFT",
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        return record

    def get(self, db: Session, record_id: str) -> PricingRecord | None:
        return db.get(PricingRecord, record_id)

    def confirm(self, db: Session, record_id: str) -> PricingRecord | None:
        record = self.get(db, record_id)
        if record is None:
            return None
        if record.status != "CONFIRMED":
            record.status = "CONFIRMED"
            record.confirmed_at = datetime.now(timezone.utc)
            db.commit()
            db.refresh(record)
        return record

    @classmethod
    def _json_value(cls, value: Any) -> Any:
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, Enum):
            return value.value
        if isinstance(value, dict):
            return {key: cls._json_value(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [cls._json_value(item) for item in value]
        return value
