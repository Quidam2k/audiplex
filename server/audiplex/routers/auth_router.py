"""Authentication endpoints — register, login, current user."""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from audiplex.auth import (
    create_token,
    decode_token,
    get_admin_user,
    get_current_user,
    hash_password,
    verify_password,
)
from audiplex.config import get_settings
from audiplex.database import get_db
from audiplex.models import User

router = APIRouter(prefix="/api/auth", tags=["auth"])


class RegisterRequest(BaseModel):
    username: str
    password: str
    display_name: str | None = None


class LoginRequest(BaseModel):
    username: str
    password: str


class UserResponse(BaseModel):
    id: int
    username: str
    display_name: str | None
    is_admin: bool
    model_config = {"from_attributes": True}


class LoginResponse(BaseModel):
    token: str
    user: UserResponse


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class ResetPasswordRequest(BaseModel):
    new_password: str


MIN_PASSWORD_LENGTH = 8


@router.post("/register", response_model=LoginResponse)
def register(body: RegisterRequest, request: Request, db: Session = Depends(get_db)):
    user_count = db.query(User).count()

    if user_count > 0:
        settings = get_settings()
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            raise HTTPException(status_code=403, detail="Admin token required for registration")
        try:
            payload = decode_token(auth[7:], settings.jwt_secret)
            admin_id = int(payload["sub"])
        except Exception:
            raise HTTPException(status_code=403, detail="Invalid token")
        admin = db.query(User).filter(User.id == admin_id).first()
        if not admin or not admin.is_admin:
            raise HTTPException(status_code=403, detail="Admin privileges required")

    existing = db.query(User).filter(User.username == body.username).first()
    if existing:
        raise HTTPException(status_code=400, detail="Username already taken")

    user = User(
        username=body.username,
        password_hash=hash_password(body.password),
        display_name=body.display_name or body.username,
        is_admin=(user_count == 0),
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    settings = get_settings()
    token = create_token(user.id, user.username, settings.jwt_secret, settings.token_expiry_hours)
    return LoginResponse(token=token, user=UserResponse.model_validate(user))


@router.post("/login", response_model=LoginResponse)
def login(body: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == body.username).first()
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    settings = get_settings()
    token = create_token(user.id, user.username, settings.jwt_secret, settings.token_expiry_hours)
    return LoginResponse(token=token, user=UserResponse.model_validate(user))


@router.get("/me", response_model=UserResponse)
def me(user: User = Depends(get_current_user)):
    return user


@router.post("/change-password", response_model=LoginResponse)
def change_password(
    body: ChangePasswordRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Change your own password. Requires the current one, so a stolen token
    alone can't lock the real owner out."""
    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(status_code=403, detail="Current password is incorrect")
    if len(body.new_password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"New password must be at least {MIN_PASSWORD_LENGTH} characters",
        )

    user.password_hash = hash_password(body.new_password)
    db.commit()
    db.refresh(user)

    # Hand back a fresh token so the caller isn't left holding one it might
    # reasonably assume was invalidated by the password change.
    settings = get_settings()
    token = create_token(user.id, user.username, settings.jwt_secret, settings.token_expiry_hours)
    return LoginResponse(token=token, user=UserResponse.model_validate(user))


@router.post("/users/{user_id}/reset-password", response_model=UserResponse)
def reset_password(
    user_id: int,
    body: ResetPasswordRequest,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Admin-only reset of another account's password, for when someone
    forgets theirs. No current-password check — that's the whole point."""
    if len(body.new_password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"New password must be at least {MIN_PASSWORD_LENGTH} characters",
        )

    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    target.password_hash = hash_password(body.new_password)
    db.commit()
    db.refresh(target)
    return UserResponse.model_validate(target)
