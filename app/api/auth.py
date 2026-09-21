from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.auth import (
    current_user,
    hash_password,
    issue_session,
    revoke_token,
    require_authenticated,
    verify_password,
)
from app.core.config import get_settings
from app.db.models import User
from app.db.session import get_db


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)


class UserRead(BaseModel):
    id: int
    username: str


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
    return {"user": UserRead(id=user.id, username=user.username)}


@router.post("/extension-login")
def extension_login(payload: LoginRequest, response: Response, db: Session = Depends(get_db)) -> dict:
    user, token = _login(db, payload)
    _set_session_cookie(response, token)
    return {"access_token": token, "user": UserRead(id=user.id, username=user.username)}


@router.get("/me", response_model=UserRead | None)
def me(user: User | None = Depends(require_authenticated)) -> User | None:
    return user


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, response: Response, db: Session = Depends(get_db)) -> None:
    token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    token = token or request.cookies.get(get_settings().auth_cookie_name)
    revoke_token(db, token)
    response.delete_cookie(get_settings().auth_cookie_name, path="/")
