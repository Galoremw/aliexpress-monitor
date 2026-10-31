from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select, update
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


def revoke_user_sessions(
    db: Session, user_id: int, *, except_token: str | None = None
) -> None:
    statement = update(AuthSession).where(
        AuthSession.user_id == user_id,
        AuthSession.revoked_at.is_(None),
    )
    if except_token:
        statement = statement.where(AuthSession.token_hash != _token_hash(except_token))
    db.execute(statement.values(revoked_at=datetime.now(timezone.utc)))


def request_token(request: Request) -> str | None:
    authorization = request.headers.get("Authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip() or None
    return request.cookies.get(get_settings().auth_cookie_name)


def session_user(request: Request, db: Session) -> User | None:
    token = request_token(request)
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


def current_user(request: Request, db: Session) -> User | None:
    if not get_settings().auth_required:
        return None
    return session_user(request, db)


def is_configured_admin_session(request: Request, db: Session) -> bool:
    admin_username = (get_settings().auth_admin_username or "").strip()
    user = session_user(request, db)
    return bool(
        admin_username
        and user
        and user.role == "admin"
        and hmac.compare_digest(user.username, admin_username)
    )


def require_admin(request: Request, db: Session = Depends(get_db)) -> User:
    admin_username = (get_settings().auth_admin_username or "").strip()
    if not admin_username:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="尚未配置监控台管理员",
        )
    user = session_user(request, db)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录监控台")
    if user.must_change_password:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="首次登录请先修改密码",
            headers={"X-Password-Change-Required": "true"},
        )
    if user.role != "admin" or not hmac.compare_digest(user.username, admin_username):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="只有监控台管理员可以执行此操作")
    return user


def require_dianxiaomi_admin(
    request: Request, db: Session = Depends(get_db)
) -> User:
    admin_username = (get_settings().auth_admin_username or "").strip()
    if not admin_username:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="尚未配置监控台管理员，不能删除店小秘队列记录",
        )
    user = session_user(request, db)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="请先使用管理员账号登录后再删除",
        )
    if user.role != "admin" or not hmac.compare_digest(user.username, admin_username):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="只有监控台管理员可以删除店小秘队列记录",
        )
    return user


def require_authenticated(request: Request, db: Session = Depends(get_db)) -> User | None:
    if not get_settings().auth_required:
        return None
    settings = get_settings()
    path = request.url.path
    authorization = request.headers.get("Authorization", "")
    scheme, separator, supplied = authorization.partition(" ")
    if separator and scheme.lower() == "bearer" and supplied.strip():
        expected = None
        if path == "/api/integrations/selection/candidates":
            expected = settings.selection_service_token
        elif path.startswith("/api/integrations/dianxiaomi/worker/"):
            expected = settings.dianxiaomi_service_token
        if expected and hmac.compare_digest(supplied.strip(), expected):
            return None
    user = current_user(request, db)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录监控台")
    if user.must_change_password and path not in {
        "/api/auth/me",
        "/api/auth/logout",
        "/api/auth/change-password",
    }:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="首次登录请先修改密码",
            headers={"X-Password-Change-Required": "true"},
        )
    return user


def require_page_auth(request: Request, db: Session = Depends(get_db)) -> User | None:
    if not get_settings().auth_required:
        request.state.user = None
        return None
    user = current_user(request, db)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            headers={"Location": "/login"},
        )
    if user.must_change_password and request.url.path != "/change-password":
        raise HTTPException(
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            headers={"Location": "/change-password"},
        )
    request.state.user = user
    return user


def ensure_bootstrap_admin(db: Session) -> None:
    settings = get_settings()
    if not settings.auth_required or not settings.auth_admin_username or not settings.auth_admin_password:
        return
    admin_username = settings.auth_admin_username.strip()
    user = db.scalar(select(User).where(User.username == admin_username))
    if user is not None:
        if user.role != "admin":
            user.role = "admin"
            db.commit()
        return
    db.add(
        User(
            username=admin_username,
            password_hash=hash_password(settings.auth_admin_password),
            role="admin",
        )
    )
    db.commit()
