"""Authentication — password hashing, JWT tokens, FastAPI dependency."""

import logging
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException, Request, Response
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from audiplex.database import get_db

logger = logging.getLogger(__name__)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# Header carrying a renewed token on responses to requests made with a token
# that is more than halfway through its life. The client swaps it in silently,
# so an app that gets used at all never hits the expiry cliff.
REFRESH_HEADER = "X-Refresh-Token"


def _reject(request: Request, reason: str, detail: str) -> HTTPException:
    """Log why a 401 is going out, then build it.

    Every 401 the client sees makes it drop the session, so a silent one is
    a session death nobody can explain after the fact (which is exactly how
    the 30-day expiry cliff went undiagnosed). One WARNING line per 401.
    """
    logger.warning(
        "401 %s %s - %s (client=%s)",
        request.method,
        request.url.path,
        reason,
        request.client.host if request.client else "?",
    )
    return HTTPException(status_code=401, detail=detail)


def hash_password(plain: str) -> str:
    return pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def create_token(user_id: int, username: str, secret: str, expires_hours: int = 720) -> str:
    payload = {
        "sub": str(user_id),
        "username": username,
        "exp": datetime.now(timezone.utc) + timedelta(hours=expires_hours),
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def decode_token(token: str, secret: str) -> dict:
    return jwt.decode(token, secret, algorithms=["HS256"])


def needs_refresh(payload: dict, expiry_hours: int) -> bool:
    """True once a token is past the halfway point of its configured life.

    Deliberately derived from the remaining time against the *configured*
    lifetime rather than an `iat` claim, so tokens minted before sliding
    renewal existed get renewed too instead of expiring out from under a
    client that never had a chance to refresh them.
    """
    exp = payload.get("exp")
    if not exp:
        return False
    remaining = datetime.fromtimestamp(exp, timezone.utc) - datetime.now(timezone.utc)
    return remaining < timedelta(hours=expiry_hours) / 2


def get_current_user(request: Request, response: Response, db: Session = Depends(get_db)):
    from audiplex.config import get_settings
    from audiplex.models import User

    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise _reject(request, "missing or malformed Bearer header", "Missing or invalid authorization header")

    token = auth_header.split(" ", 1)[1]
    settings = get_settings()
    try:
        payload = decode_token(token, settings.jwt_secret)
    except jwt.ExpiredSignatureError:
        raise _reject(request, "token expired", "Token expired")
    except jwt.InvalidTokenError as e:
        raise _reject(request, f"invalid token ({type(e).__name__})", "Invalid token")

    try:
        user_id = int(payload["sub"])
    except (KeyError, TypeError, ValueError):
        raise _reject(request, "token has no usable sub claim", "Invalid token")

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise _reject(request, f"token references missing user id={user_id}", "User not found")

    if needs_refresh(payload, settings.token_expiry_hours):
        response.headers[REFRESH_HEADER] = create_token(
            user.id, user.username, settings.jwt_secret, settings.token_expiry_hours
        )

    return user


def get_admin_user(user=Depends(get_current_user)):
    """Dependency for endpoints that mutate server config or trigger
    expensive work (rescans, bulk enrichment)."""
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin privileges required")
    return user
