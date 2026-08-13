"""DJ voice-break clip endpoints (item #431)."""

import time
import wave
from pathlib import Path

import pytest

from audiplex.config import get_settings
from audiplex.routers import dj_voice


@pytest.fixture
def clip_dir(tmp_path, monkeypatch):
    """Point dj_clip_dir at a temp dir without touching the real config.yaml."""
    d = tmp_path / "dj_clips"
    settings = get_settings()
    monkeypatch.setattr(settings, "dj_clip_dir", str(d), raising=False)
    return d


def _wav_bytes(seconds: float = 0.25, framerate: int = 8000) -> bytes:
    """A real (silent) WAV so mutagen can actually probe a duration."""
    path = Path(__file__).parent / f"_tmp_clip_{time.time_ns()}.wav"
    try:
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(framerate)
            w.writeframes(b"\x00\x00" * int(framerate * seconds))
        return path.read_bytes()
    finally:
        path.unlink(missing_ok=True)


def _upload(client, data: bytes, filename: str = "break.wav", title: str = "DJ break"):
    return client.post(
        "/api/dj/clips",
        files={"file": (filename, data, "audio/wav")},
        data={"title": title},
    )


def test_upload_returns_clip_id_url_and_duration(client, clip_dir):
    resp = _upload(client, _wav_bytes(0.5))
    assert resp.status_code == 200
    body = resp.json()
    assert body["clip_id"] > 0
    assert body["url"] == f"/api/dj/clips/{body['clip_id']}"
    # Probed via mutagen — the client uses this to know how long the break runs.
    assert body["duration_seconds"] == pytest.approx(0.5, abs=0.1)
    assert (clip_dir / f"{body['clip_id']}.wav").is_file()


def test_upload_then_fetch_roundtrip(client, clip_dir):
    data = _wav_bytes()
    clip_id = _upload(client, data).json()["clip_id"]

    resp = client.get(f"/api/dj/clips/{clip_id}")
    assert resp.status_code == 200
    assert resp.content == data
    assert resp.headers["content-type"] == "audio/wav"
    # Range support matters: ExoPlayer issues ranged reads.
    assert resp.headers.get("accept-ranges") == "bytes"


def test_fetch_supports_range_requests(client, clip_dir):
    data = _wav_bytes()
    clip_id = _upload(client, data).json()["clip_id"]

    resp = client.get(f"/api/dj/clips/{clip_id}", headers={"Range": "bytes=0-99"})
    assert resp.status_code == 206
    assert resp.content == data[:100]


def test_fetch_missing_clip_404s(client, clip_dir):
    assert client.get("/api/dj/clips/1").status_code == 404


def test_rejects_unsupported_extension(client, clip_dir):
    resp = _upload(client, b"not audio", filename="break.txt")
    assert resp.status_code == 400
    assert "Unsupported clip type" in resp.json()["detail"]


def test_rejects_empty_upload(client, clip_dir):
    resp = _upload(client, b"")
    assert resp.status_code == 400
    assert "Empty clip" in resp.json()["detail"]


def test_rejects_oversized_upload(client, clip_dir, monkeypatch):
    monkeypatch.setattr(dj_voice, "MAX_CLIP_BYTES", 10)
    resp = _upload(client, _wav_bytes())
    assert resp.status_code == 413


def test_upload_prunes_clips_past_ttl(client, clip_dir):
    """A break is stale the moment the next song ends, so old clips get swept."""
    first = _upload(client, _wav_bytes()).json()["clip_id"]
    stale = clip_dir / f"{first}.wav"
    assert stale.is_file()

    old = time.time() - (dj_voice.CLIP_TTL_DAYS + 1) * 86400
    import os

    os.utime(stale, (old, old))

    fresh = _upload(client, _wav_bytes()).json()["clip_id"]
    assert not stale.exists(), "clip past its TTL should have been pruned"
    assert (clip_dir / f"{fresh}.wav").is_file(), "the new clip must survive its own prune"


def test_same_millisecond_uploads_do_not_collide(client, clip_dir, monkeypatch):
    """Two clips minted in the same millisecond must not overwrite each other —
    the loser would be a break already queued for playback."""
    monkeypatch.setattr(dj_voice.time, "time", lambda: 1_700_000_000.0)
    a = _upload(client, _wav_bytes()).json()["clip_id"]
    b = _upload(client, _wav_bytes()).json()["clip_id"]
    assert a != b
    assert (clip_dir / f"{a}.wav").is_file()
    assert (clip_dir / f"{b}.wav").is_file()


def test_clip_endpoints_require_auth(db_engine, clip_dir):
    """Without the auth override, both endpoints must reject an anonymous caller."""
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import sessionmaker

    from audiplex.database import get_db
    from tests.conftest import _create_test_app

    app = _create_test_app()
    TestSession = sessionmaker(bind=db_engine)

    def override_get_db():
        session = TestSession()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db

    with TestClient(app) as c:
        assert c.post("/api/dj/clips", files={"file": ("b.wav", b"x", "audio/wav")}).status_code == 401
        assert c.get("/api/dj/clips/1").status_code == 401
