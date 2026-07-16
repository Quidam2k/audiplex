"""Tests for /api/app/* auth enforcement.

Codex flagged (2026-07-16, item #535) that these endpoints imported the
auth dependency but never actually declared it as a parameter, so they were
effectively unauthenticated. This suite builds its own TestClient WITHOUT
overriding get_current_user (conftest's shared `client` fixture always
bypasses auth) so a missing/invalid token really produces a 401.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from audiplex.auth import create_token
from audiplex.config import get_settings
from audiplex.database import get_db
from audiplex.models import User
from audiplex.routers import app as app_router


def _real_auth_client(db_engine):
    test_app = FastAPI()
    test_app.include_router(app_router.router)
    TestSession = sessionmaker(bind=db_engine)

    def override_get_db():
        session = TestSession()
        try:
            yield session
        finally:
            session.close()

    test_app.dependency_overrides[get_db] = override_get_db
    return TestClient(test_app)


class TestAppAuthRequired:
    def test_version_without_token_401s(self, db_engine):
        resp = _real_auth_client(db_engine).get("/api/app/version")
        assert resp.status_code == 401

    def test_download_without_token_401s(self, db_engine):
        resp = _real_auth_client(db_engine).get("/api/app/download")
        assert resp.status_code == 401

    def test_version_with_garbage_token_401s(self, db_engine):
        resp = _real_auth_client(db_engine).get(
            "/api/app/version", headers={"Authorization": "Bearer not-a-real-token"}
        )
        assert resp.status_code == 401

    def test_version_with_valid_token_passes_auth(self, db_engine, db_session, tmp_path, monkeypatch):
        # No APK metadata under tmp_path, so this 404s past auth rather than
        # 200 — the point is proving auth is enforced (401 would fire first
        # if the dependency weren't declared), not exercising the full
        # download flow (already covered by manual/on-device use).
        settings = get_settings()
        monkeypatch.setattr(settings, "apk_output_dir", str(tmp_path))
        user = db_session.query(User).first()
        token = create_token(user.id, user.username, settings.jwt_secret, 1)

        resp = _real_auth_client(db_engine).get(
            "/api/app/version", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 404

    def test_download_with_valid_token_passes_auth(self, db_engine, db_session, tmp_path, monkeypatch):
        settings = get_settings()
        monkeypatch.setattr(settings, "apk_output_dir", str(tmp_path))
        user = db_session.query(User).first()
        token = create_token(user.id, user.username, settings.jwt_secret, 1)

        resp = _real_auth_client(db_engine).get(
            "/api/app/download", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 404
