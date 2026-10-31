from __future__ import annotations

import secrets

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.auth import hash_password, revoke_user_sessions, verify_password
from app.core.config import get_settings
from app.db.models import User

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 256


def _new_temporary_password() -> str:
    return secrets.token_urlsafe(24)


def _require_operator_target(user: User | None) -> User:
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="账号不存在")
    configured_admin = (get_settings().auth_admin_username or "").strip()
    if user.role == "admin" or user.username == configured_admin:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="不能重置或停用管理员账号")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="账号已停用")
    return user


def list_users(db: Session) -> list[User]:
    return list(
        db.scalars(
            select(User)
            .where(User.role == "operator")
            .order_by(User.created_at, User.id)
        )
    )


def create_operator(db: Session, username: str) -> tuple[User, str]:
    normalized_username = username.strip()
    if not normalized_username:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="账号不能为空")
    if len(normalized_username) > 128:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="账号最多 128 个字符")
    configured_admin = (get_settings().auth_admin_username or "").strip()
    if normalized_username == configured_admin:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="该账号名保留给监控台管理员")
    if db.scalar(select(User.id).where(User.username == normalized_username)) is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="账号已存在")

    temporary_password = _new_temporary_password()
    user = User(
        username=normalized_username,
        password_hash=hash_password(temporary_password),
        role="operator",
        is_active=True,
        must_change_password=True,
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="账号已存在") from error
    db.refresh(user)
    return user, temporary_password


def reset_operator_password(db: Session, user_id: int) -> tuple[User, str]:
    user = _require_operator_target(db.get(User, user_id))
    temporary_password = _new_temporary_password()
    user.password_hash = hash_password(temporary_password)
    user.must_change_password = True
    revoke_user_sessions(db, user.id)
    db.commit()
    db.refresh(user)
    return user, temporary_password


def disable_operator(db: Session, user_id: int) -> User:
    user = _require_operator_target(db.get(User, user_id))
    user.is_active = False
    revoke_user_sessions(db, user.id)
    db.commit()
    db.refresh(user)
    return user


def change_password(
    db: Session,
    user: User,
    *,
    current_password: str,
    new_password: str,
    current_token: str | None,
) -> User:
    if not verify_password(current_password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="当前密码不正确")
    if not MIN_PASSWORD_LENGTH <= len(new_password) <= MAX_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"新密码长度需为 {MIN_PASSWORD_LENGTH} 到 {MAX_PASSWORD_LENGTH} 个字符",
        )
    if secrets.compare_digest(current_password, new_password):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="新密码不能与当前密码相同",
        )

    user.password_hash = hash_password(new_password)
    user.must_change_password = False
    revoke_user_sessions(db, user.id, except_token=current_token)
    db.commit()
    db.refresh(user)
    return user
