"""Auth API tests — real token verification (no get_current_user override),
sliding refresh, and the password-change/reset paths.

The shared `client` fixture in conftest overrides get_current_user to bypass
auth, which is exactly what must NOT happen here: these tests exist to prove
the token path itself works, so they build their own client that only
overrides the database.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import jwt
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from audiplex.auth import REFRESH_HEADER, hash_password, needs_refresh
from audiplex.database import Base, get_db
from audiplex.models import User
from audiplex.routers import auth_router

JWT_SECRET = "test-secret-not-the-real-one"
EXPIRY_HOURS = 720


class FakeSettings:
    jwt_secret = JWT_SECRET
    token_expiry_hours = EXPIRY_HOURS
    dj_owner_username = "boss"


@pytest.fixture
def auth_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    session.add(User(
        username="boss",
        password_hash=hash_password("boss-password"),
        display_name="Boss",
        is_admin=True,
    ))
    session.add(User(
        username="peon",
        password_hash=hash_password("peon-password"),
        display_name="Peon",
        is_admin=False,
    ))
    session.commit()
    session.close()
    return engine


@pytest.fixture
def auth_client(auth_engine):
    """TestClient with a real auth path — only get_db is overridden."""
    app = FastAPI()
    app.include_router(auth_router.router)
    TestSession = sessionmaker(bind=auth_engine)

    def override_get_db():
        session = TestSession()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db

    # auth.py imports get_settings inside the function (so patching the source
    # module reaches it), but auth_router.py binds it at import time — patch
    # both or the router signs tokens with the real secret while auth.py
    # verifies them with the fake one.
    fake = FakeSettings()
    with patch("audiplex.config.get_settings", return_value=fake), \
         patch("audiplex.routers.auth_router.get_settings", return_value=fake):
        with TestClient(app) as c:
            yield c


def _token_for(user_id: int, username: str, expires_in: timedelta) -> str:
    return jwt.encode(
        {
            "sub": str(user_id),
            "username": username,
            "exp": datetime.now(timezone.utc) + expires_in,
        },
        JWT_SECRET,
        algorithm="HS256",
    )


def _login(client, username, password):
    return client.post("/api/auth/login", json={"username": username, "password": password})


# --- login / token verification -------------------------------------------

def test_login_succeeds_and_token_authenticates(auth_client):
    resp = _login(auth_client, "boss", "boss-password")
    assert resp.status_code == 200
    token = resp.json()["token"]

    me = auth_client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["username"] == "boss"


def test_login_with_wrong_password_is_401(auth_client):
    """This 401 must never be read as 'the stored session died' — it is the
    login endpoint rejecting one attempt. The Android interceptor exempts
    /api/auth/login from its token-clearing path for exactly this reason."""
    assert _login(auth_client, "boss", "wrong").status_code == 401


def test_missing_and_malformed_headers_are_401(auth_client):
    assert auth_client.get("/api/auth/me").status_code == 401
    assert auth_client.get(
        "/api/auth/me", headers={"Authorization": "boss-password"}
    ).status_code == 401


def test_expired_token_is_401_with_expired_detail(auth_client):
    expired = _token_for(1, "boss", timedelta(hours=-1))
    resp = auth_client.get("/api/auth/me", headers={"Authorization": f"Bearer {expired}"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Token expired"


def test_token_signed_with_another_secret_is_401(auth_client):
    forged = jwt.encode(
        {"sub": "1", "exp": datetime.now(timezone.utc) + timedelta(hours=1)},
        "some-other-secret",
        algorithm="HS256",
    )
    resp = auth_client.get("/api/auth/me", headers={"Authorization": f"Bearer {forged}"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid token"


def test_token_for_deleted_user_is_401(auth_client):
    ghost = _token_for(9999, "ghost", timedelta(hours=100))
    resp = auth_client.get("/api/auth/me", headers={"Authorization": f"Bearer {ghost}"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "User not found"


# --- sliding refresh ------------------------------------------------------

def test_needs_refresh_only_past_halfway():
    now = datetime.now(timezone.utc)
    fresh = {"exp": (now + timedelta(hours=EXPIRY_HOURS)).timestamp()}
    half_spent = {"exp": (now + timedelta(hours=EXPIRY_HOURS / 2 - 1)).timestamp()}
    assert needs_refresh(fresh, EXPIRY_HOURS) is False
    assert needs_refresh(half_spent, EXPIRY_HOURS) is True
    assert needs_refresh({}, EXPIRY_HOURS) is False


def test_fresh_token_gets_no_refresh_header(auth_client):
    token = _login(auth_client, "boss", "boss-password").json()["token"]
    resp = auth_client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert REFRESH_HEADER not in resp.headers


def test_half_spent_token_is_renewed_and_renewal_works(auth_client):
    """The point of the whole exercise: a token past halfway comes back
    renewed, so an app that gets used never reaches the expiry cliff."""
    old = _token_for(1, "boss", timedelta(hours=EXPIRY_HOURS / 4))
    resp = auth_client.get("/api/auth/me", headers={"Authorization": f"Bearer {old}"})
    assert resp.status_code == 200

    renewed = resp.headers.get(REFRESH_HEADER)
    assert renewed and renewed != old

    # The renewed token authenticates, and is further from expiry than the old.
    again = auth_client.get("/api/auth/me", headers={"Authorization": f"Bearer {renewed}"})
    assert again.status_code == 200
    assert again.json()["username"] == "boss"
    assert REFRESH_HEADER not in again.headers

    old_exp = jwt.decode(old, JWT_SECRET, algorithms=["HS256"])["exp"]
    new_exp = jwt.decode(renewed, JWT_SECRET, algorithms=["HS256"])["exp"]
    assert new_exp > old_exp


def test_expired_token_is_not_renewed(auth_client):
    expired = _token_for(1, "boss", timedelta(hours=-1))
    resp = auth_client.get("/api/auth/me", headers={"Authorization": f"Bearer {expired}"})
    assert resp.status_code == 401
    assert REFRESH_HEADER not in resp.headers


# --- change own password --------------------------------------------------

def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_change_password_replaces_credential(auth_client):
    token = _login(auth_client, "peon", "peon-password").json()["token"]
    resp = auth_client.post(
        "/api/auth/change-password",
        json={"current_password": "peon-password", "new_password": "brand-new-password"},
        headers=_auth(token),
    )
    assert resp.status_code == 200

    assert _login(auth_client, "peon", "peon-password").status_code == 401
    assert _login(auth_client, "peon", "brand-new-password").status_code == 200

    # The token handed back by change-password must itself work.
    new_token = resp.json()["token"]
    assert auth_client.get("/api/auth/me", headers=_auth(new_token)).status_code == 200


def test_change_password_requires_correct_current_password(auth_client):
    token = _login(auth_client, "peon", "peon-password").json()["token"]
    resp = auth_client.post(
        "/api/auth/change-password",
        json={"current_password": "not-it", "new_password": "brand-new-password"},
        headers=_auth(token),
    )
    assert resp.status_code == 403
    assert _login(auth_client, "peon", "peon-password").status_code == 200


def test_change_password_rejects_short_password(auth_client):
    token = _login(auth_client, "peon", "peon-password").json()["token"]
    resp = auth_client.post(
        "/api/auth/change-password",
        json={"current_password": "peon-password", "new_password": "short"},
        headers=_auth(token),
    )
    assert resp.status_code == 400
    assert _login(auth_client, "peon", "peon-password").status_code == 200


def test_change_password_requires_authentication(auth_client):
    resp = auth_client.post(
        "/api/auth/change-password",
        json={"current_password": "peon-password", "new_password": "brand-new-password"},
    )
    assert resp.status_code == 401


# --- admin reset of someone else's password ------------------------------

def test_admin_can_reset_another_users_password(auth_client):
    admin_token = _login(auth_client, "boss", "boss-password").json()["token"]
    resp = auth_client.post(
        "/api/auth/users/2/reset-password",
        json={"new_password": "reset-by-admin"},
        headers=_auth(admin_token),
    )
    assert resp.status_code == 200
    assert resp.json()["username"] == "peon"
    assert _login(auth_client, "peon", "reset-by-admin").status_code == 200


def test_non_admin_cannot_reset_passwords(auth_client):
    peon_token = _login(auth_client, "peon", "peon-password").json()["token"]
    resp = auth_client.post(
        "/api/auth/users/1/reset-password",
        json={"new_password": "privilege-escalation"},
        headers=_auth(peon_token),
    )
    assert resp.status_code == 403
    # The admin's credential is untouched.
    assert _login(auth_client, "boss", "boss-password").status_code == 200


def test_reset_password_unknown_user_is_404(auth_client):
    admin_token = _login(auth_client, "boss", "boss-password").json()["token"]
    resp = auth_client.post(
        "/api/auth/users/4242/reset-password",
        json={"new_password": "reset-by-admin"},
        headers=_auth(admin_token),
    )
    assert resp.status_code == 404


def test_reset_password_rejects_short_password(auth_client):
    admin_token = _login(auth_client, "boss", "boss-password").json()["token"]
    resp = auth_client.post(
        "/api/auth/users/2/reset-password",
        json={"new_password": "short"},
        headers=_auth(admin_token),
    )
    assert resp.status_code == 400
    assert _login(auth_client, "peon", "peon-password").status_code == 200


def test_reset_password_requires_authentication(auth_client):
    resp = auth_client.post(
        "/api/auth/users/2/reset-password", json={"new_password": "reset-by-admin"}
    )
    assert resp.status_code == 401
