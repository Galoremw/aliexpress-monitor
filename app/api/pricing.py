"""Authenticated pricing APIs and a private token-protected service API."""

import secrets
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from app.api.pricing_schemas import PackagingPreviewInput, PricingQuoteInput
from app.core.auth import require_authenticated
from app.core.config import Settings, get_settings
from app.db.models import PricingRecord
from app.db.session import get_db
from app.services.pricing import PricingService
from app.services.pricing_repository import PricingRepository


pricing_service = PricingService()
pricing_repository = PricingRepository()


def require_pricing_service_token(
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    expected = settings.pricing_service_token
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="pricing service token is not configured",
        )
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid service credentials",
        )
    scheme, separator, supplied = authorization.partition(" ")
    if (
        not separator
        or scheme.lower() != "bearer"
        or not supplied.strip()
        or not secrets.compare_digest(supplied.strip(), expected)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid service credentials",
        )


router = APIRouter()
user_router = APIRouter(
    prefix="/api/pricing",
    dependencies=[Depends(require_authenticated)],
)
internal_router = APIRouter(
    prefix="/api/internal/pricing",
    dependencies=[Depends(require_pricing_service_token)],
)


@user_router.post("/quote")
def quote_for_user(payload: PricingQuoteInput) -> dict[str, Any]:
    return pricing_service.quote(payload)


@user_router.post("/packaging-preview")
def packaging_preview_for_user(payload: PackagingPreviewInput) -> dict[str, Any]:
    return pricing_service.preview_packaging(payload)


@user_router.get("/cartons")
def cartons_for_user() -> list[dict[str, Any]]:
    return pricing_service.packaging_options()


@user_router.post("/records")
def save_user_pricing_record(
    payload: PricingQuoteInput,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    quote = pricing_service.quote(payload)
    if quote["status"] != "QUOTED":
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=quote)
    pricing_repository.ensure_rule_versions(db)
    try:
        record = pricing_repository.save(db, payload, quote)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return _serialize_record(record)


@user_router.get("/records/{record_id}")
def get_user_pricing_record(
    record_id: str,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    record = pricing_repository.get(db, record_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="pricing record not found")
    return _serialize_record(record)


@user_router.post("/records/{record_id}/confirm")
def confirm_user_pricing_record(
    record_id: str,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    record = pricing_repository.confirm(db, record_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="pricing record not found")
    return _serialize_record(record)


@internal_router.post("/quote")
def quote_for_service(payload: PricingQuoteInput) -> dict[str, Any]:
    return pricing_service.quote(payload)


@internal_router.post("/packaging-preview")
def packaging_preview_for_service(payload: PackagingPreviewInput) -> dict[str, Any]:
    return pricing_service.preview_packaging(payload)


@internal_router.get("/cartons")
def cartons_for_service() -> list[dict[str, Any]]:
    return pricing_service.packaging_options()


def _serialize_record(record: PricingRecord) -> dict[str, Any]:
    return {
        "record_id": record.record_id,
        "product_id": record.product_id,
        "product_name": record.product_name,
        "sku": record.sku,
        "rule_version": record.rule_version,
        "input": record.input_json,
        "result": record.result_json,
        "final_price": record.final_price,
        "status": record.status,
        "created_at": record.created_at,
        "confirmed_at": record.confirmed_at,
    }


router.include_router(user_router)
router.include_router(internal_router)
