from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.auth import (
    issue_session,
    revoke_token,
    request_token,
    require_admin,
    require_authenticated,
    verify_password,
)
from app.core.config import get_settings
from app.db.models import User
from app.db.session import get_db
from app.services.user_accounts import (
    MAX_PASSWORD_LENGTH,
    MIN_PASSWORD_LENGTH,
    change_password,
    create_operator,
    disable_operator,
    list_users,
    reset_operator_password,
)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)


class AuthUserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    role: str
    must_change_password: bool


class UserManagementRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    role: str
    is_active: bool
    must_change_password: bool
    created_at: datetime


class CreateOperatorRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)


class UserCredentialResponse(BaseModel):
    user: UserManagementRead
    temporary_password: str


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)
    new_password: str = Field(
        min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH
    )


router = APIRouter(prefix="/api/auth", tags=["auth"])


def _login(db: Session, payload: LoginRequest) -> tuple[User, str]:
    user = db.scalar(select(User).where(User.username == payload.username.strip()))
    if user is None or not user.is_active or not verify_password(payload.password, user.password_hash):
        from fastapi import HTTPException

        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="账号或密码错误")
    token, _ = issue_session(db, user)
    return user, token


def _set_session_cookie(response: Response, token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        key=settings.auth_cookie_name,
        value=token,
        max_age=settings.auth_session_ttl_hours * 3600,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite=settings.auth_cookie_samesite,
        path="/",
    )


@router.post("/login")
def login(payload: LoginRequest, response: Response, db: Session = Depends(get_db)) -> dict:
    user, _token = _login(db, payload)
    _set_session_cookie(response, _token)
    return {"user": AuthUserRead.model_validate(user)}


@router.post("/extension-login")
def extension_login(payload: LoginRequest, response: Response, db: Session = Depends(get_db)) -> dict:
    user, token = _login(db, payload)
    if user.must_change_password:
        revoke_token(db, token)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="请先在监控台登录并修改初始密码",
        )
    _set_session_cookie(response, token)
    return {"access_token": token, "user": AuthUserRead.model_validate(user)}


@router.get("/me", response_model=AuthUserRead | None)
def me(user: User | None = Depends(require_authenticated)) -> User | None:
    return user


@router.get("/users", response_model=list[UserManagementRead])
def users(
    _admin: User = Depends(require_admin), db: Session = Depends(get_db)
) -> list[User]:
    return list_users(db)


@router.post("/users", response_model=UserCredentialResponse, status_code=status.HTTP_201_CREATED)
def create_user(
    payload: CreateOperatorRequest,
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    user, temporary_password = create_operator(db, payload.username)
    return {"user": user, "temporary_password": temporary_password}


@router.post("/users/{user_id}/reset-password", response_model=UserCredentialResponse)
def reset_user_password(
    user_id: int,
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    user, temporary_password = reset_operator_password(db, user_id)
    return {"user": user, "temporary_password": temporary_password}


@router.post("/users/{user_id}/disable", response_model=UserManagementRead)
def disable_user(
    user_id: int,
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> User:
    return disable_operator(db, user_id)


@router.post("/change-password", response_model=AuthUserRead)
def change_current_password(
    payload: ChangePasswordRequest,
    request: Request,
    user: User | None = Depends(require_authenticated),
    db: Session = Depends(get_db),
) -> User:
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录监控台")
    return change_password(
        db,
        user,
        current_password=payload.current_password,
        new_password=payload.new_password,
        current_token=request_token(request),
    )


@router.get("/extension-token")
def extension_token(
    user: User | None = Depends(require_authenticated),
    db: Session = Depends(get_db),
) -> dict:
    """Issue a scoped bearer session for the installed browser extension.

    The dashboard bridge calls this same-origin endpoint with the user's
    existing dashboard cookie. The token is then handed to the extension
    service worker without reading cookies or browser storage.
    """
    if user is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录监控台")
    token, _ = issue_session(db, user)
    return {"access_token": token, "user": AuthUserRead.model_validate(user)}


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, response: Response, db: Session = Depends(get_db)) -> None:
    revoke_token(db, request_token(request))
    response.delete_cookie(get_settings().auth_cookie_name, path="/")
