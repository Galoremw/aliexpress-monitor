"""Service-token dependencies for private project integrations."""

import secrets

from fastapi import Header, HTTPException, status

from app.core.config import Settings, get_settings


def _require_token(supplied_authorization: str | None, expected: str | None, service: str) -> None:
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"{service} service token is not configured",
        )
    scheme, separator, supplied = (supplied_authorization or "").partition(" ")
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


def require_selection_service_token(
    authorization: str | None = Header(default=None),
) -> None:
    _require_token(authorization, get_settings().selection_service_token, "selection")


def require_dianxiaomi_service_token(
    authorization: str | None = Header(default=None),
) -> None:
    _require_token(authorization, get_settings().dianxiaomi_service_token, "Dianxiaomi")
