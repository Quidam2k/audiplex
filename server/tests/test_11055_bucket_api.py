"""#11055: DJ bucket REST API for the browser UI (store: audiplex_mcp/buckets.py)."""

import sys
from pathlib import Path

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from audiplex.auth import get_current_user, hash_password
from audiplex.database import get_db
from audiplex.models import User
from audiplex.routers import buckets as buckets_router

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from audiplex_mcp import buckets

BASE = "/api/dj/buckets"


@pytest.fixture(autouse=True)
def isolated_bucket_store(monkeypatch, tmp_path):
    monkeypatch.setenv("DJ_BUCKETS_DB", str(tmp_path / "b.db"))


def _client(db_engine):
    Session_ = sessionmaker(bind=db_engine)
    with Session_() as s:
        s.add(
            User(
                username="u",
                password_hash=hash_password("pw"),
                display_name="U",
                is_admin=True,
            )
        )
        s.commit()

    app = FastAPI()
    app.include_router(buckets_router.router)

    def db():
        s = Session_()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = db
    app.dependency_overrides[get_current_user] = (
        lambda d=Depends(get_db): d.query(User).filter_by(username="u").one()
    )
    return TestClient(app)


@pytest.fixture
def client(db_engine):
    with _client(db_engine) as test_client:
        yield test_client


def test_create_list_duplicate_and_blank_name(client):
    response = client.post(BASE, json={"name": "Warmup"})
    assert response.status_code == 201
    assert response.json()["name"] == "warmup"
    assert response.json()["created"] is True

    response = client.get(BASE)
    assert response.status_code == 200
    assert len(response.json()) == 1
    assert response.json()[0]["name"] == "warmup"
    assert response.json()[0]["track_count"] == 0

    response = client.post(BASE, json={"name": "  WARMUP   "})
    assert response.status_code == 409
    assert response.json() == {
        "detail": "A bucket named 'warmup' already exists."
    }

    response = client.post(BASE, json={"name": "   "})
    assert response.status_code == 400


def test_add_tracks_reports_unknown_ids_and_duplicates(client, sample_track, db_session):
    db_session.commit()
    assert client.post(BASE, json={"name": "warmup"}).status_code == 201

    response = client.post(
        f"{BASE}/warmup/tracks",
        json={"track_ids": [sample_track.id, sample_track.id, 999999]},
    )
    assert response.status_code == 200
    assert response.json()["added"] == 1
    assert response.json()["track_count"] == 1
    assert response.json()["unknown_ids"] == [999999]

    response = client.post(
        f"{BASE}/warmup/tracks", json={"track_ids": [sample_track.id]}
    )
    assert response.status_code == 200
    assert response.json()["added"] == 0
    assert response.json()["skipped_duplicates"] == 1


@pytest.mark.parametrize("track_ids", [[], [999999]])
def test_add_empty_or_all_unknown_tracks(client, track_ids):
    assert client.post(BASE, json={"name": "warmup"}).status_code == 201

    response = client.post(
        f"{BASE}/warmup/tracks", json={"track_ids": track_ids}
    )
    assert response.status_code == 200
    assert response.json()["added"] == 0
    assert response.json()["track_count"] == 0
    assert response.json()["unknown_ids"] == track_ids


def test_get_enriches_tracks_and_preserves_missing_rows(client, sample_track, db_session):
    db_session.commit()
    assert client.post(BASE, json={"name": "warmup"}).status_code == 201
    response = client.post(
        f"{BASE}/warmup/tracks", json={"track_ids": [sample_track.id]}
    )
    assert response.status_code == 200
    buckets.add_tracks("warmup", [{"track_id": 424242, "path": "x/Gone.mp3"}])

    response = client.get(f"{BASE}/warmup")
    assert response.status_code == 200
    rows = response.json()["tracks"]
    assert [row["track_id"] for row in rows] == [sample_track.id, 424242]
    assert rows[0]["title"] == "Test Track"
    assert rows[0]["id"] == sample_track.id
    assert rows[0]["artist_name"] == "Test Artist"
    assert rows[0]["album"] == "Test Album"
    assert rows[0]["duration_seconds"] == sample_track.duration_seconds
    assert rows[0]["path"] == sample_track.file_path
    assert rows[0]["missing"] is False
    assert rows[1]["title"] == "Gone.mp3"
    assert rows[1]["artist_name"] is None
    assert rows[1]["album"] is None
    assert rows[1]["duration_seconds"] == 0
    assert rows[1]["missing"] is True


def test_remove_tracks_with_delete_body(client, sample_track, db_session):
    db_session.commit()
    assert client.post(BASE, json={"name": "warmup"}).status_code == 201
    assert client.post(
        f"{BASE}/warmup/tracks", json={"track_ids": [sample_track.id]}
    ).status_code == 200

    response = client.request(
        "DELETE",
        f"{BASE}/warmup/tracks",
        json={"track_ids": [sample_track.id]},
    )
    assert response.status_code == 200
    assert response.json()["removed"] == 1
    assert response.json()["track_count"] == 0


def test_update_description_and_reject_blank(client):
    assert client.post(
        BASE, json={"name": "warmup", "description": "Original"}
    ).status_code == 201

    response = client.put(
        f"{BASE}/warmup", json={"description": "Updated"}
    )
    assert response.status_code == 200
    assert response.json()["description"] == "Updated"
    assert response.json()["origin"] == "planned"
    assert response.json()["created_by"] == "u"
    assert client.get(f"{BASE}/warmup").json()["description"] == "Updated"

    response = client.put(f"{BASE}/warmup", json={"description": ""})
    assert response.status_code == 400
    assert response.json() == {"detail": "Description can't be blank."}
    assert client.get(f"{BASE}/warmup").json()["description"] == "Updated"


def test_delete_and_missing_bucket_operations(client):
    assert client.post(BASE, json={"name": "warmup"}).status_code == 201

    response = client.delete(f"{BASE}/warmup")
    assert response.status_code == 200
    assert response.json() == {"deleted": True}
    assert client.get(f"{BASE}/warmup").status_code == 404
    assert client.delete(f"{BASE}/warmup").status_code == 404
    assert client.post(
        f"{BASE}/warmup/tracks", json={"track_ids": []}
    ).status_code == 404
    assert client.request(
        "DELETE", f"{BASE}/warmup/tracks", json={"track_ids": []}
    ).status_code == 404
    assert client.put(
        f"{BASE}/warmup", json={"description": "Updated"}
    ).status_code == 404


def test_url_encoded_bucket_name(client):
    assert client.post(BASE, json={"name": "Ritz Warmup"}).status_code == 201

    response = client.get(f"{BASE}/ritz%20warmup")
    assert response.status_code == 200
    assert response.json()["name"] == "ritz warmup"
    assert response.json()["tracks"] == []


def test_windows_path_basename_for_missing_track(client):
    assert client.post(BASE, json={"name": "warmup"}).status_code == 201
    buckets.add_tracks("warmup", [{"track_id": 515151, "path": r"M:\Music\Gone Too.flac"}])
    assert client.get(f"{BASE}/warmup").json()["tracks"][0]["title"] == "Gone Too.flac"
