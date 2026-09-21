from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import AuthSession, User
from app.db.session import get_db


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=64
    )
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, salt_hex, digest_hex = encoded.split("$", 2)
        if algorithm != "scrypt":
            return False
        expected = hashlib.scrypt(
            password.encode("utf-8"), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1, dklen=64
        )
        return hmac.compare_digest(expected.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def _token_hash(token: str) -> str:
    secret = get_settings().auth_session_secret.encode("utf-8")
    return hmac.new(secret, token.encode("utf-8"), hashlib.sha256).hexdigest()


def issue_session(db: Session, user: User) -> tuple[str, AuthSession]:
    token = secrets.token_urlsafe(32)
    session = AuthSession(
        user_id=user.id,
        token_hash=_token_hash(token),
        expires_at=datetime.now(timezone.utc)
        + timedelta(hours=get_settings().auth_session_ttl_hours),
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return token, session


def revoke_token(db: Session, token: str | None) -> None:
    if not token:
        return
    session = db.scalar(select(AuthSession).where(AuthSession.token_hash == _token_hash(token)))
    if session and session.revoked_at is None:
        session.revoked_at = datetime.now(timezone.utc)
        db.commit()


def _request_token(request: Request) -> str | None:
    authorization = request.headers.get("Authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip() or None
    return request.cookies.get(get_settings().auth_cookie_name)


def current_user(request: Request, db: Session) -> User | None:
    if not get_settings().auth_required:
        return None
    token = _request_token(request)
    if not token:
        return None
    session = db.scalar(
        select(AuthSession)
        .where(
            AuthSession.token_hash == _token_hash(token),
            AuthSession.revoked_at.is_(None),
            AuthSession.expires_at > datetime.now(timezone.utc),
        )
    )
    if session is None or not session.user.is_active:
        return None
    return session.user


def require_authenticated(request: Request, db: Session = Depends(get_db)) -> User | None:
    if not get_settings().auth_required:
        return None
    user = current_user(request, db)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录监控台")
    return user


def require_page_auth(request: Request, db: Session = Depends(get_db)) -> User | None:
    if not get_settings().auth_required:
        return None
    user = current_user(request, db)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            headers={"Location": "/login"},
        )
    return user


def ensure_bootstrap_admin(db: Session) -> None:
    settings = get_settings()
    if not settings.auth_required or not settings.auth_admin_username or not settings.auth_admin_password:
        return
    if db.scalar(select(User.id).limit(1)) is not None:
        return
    db.add(
        User(
            username=settings.auth_admin_username.strip(),
            password_hash=hash_password(settings.auth_admin_password),
        )
    )
    db.commit()
