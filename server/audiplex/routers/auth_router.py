"""Authentication endpoints — register, login, current user."""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from audiplex.auth import (
    create_token,
    decode_token,
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
