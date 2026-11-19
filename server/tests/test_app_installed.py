"""#4456: the server remembers which build each phone runs and serves it at /api/app/installed."""

import json

import pytest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from audiplex import app_installs
from audiplex.auth import get_current_user
from audiplex.config import get_settings
from audiplex.routers import app as app_router


@pytest.fixture
def client():
    test_app = FastAPI()
    test_app.include_router(app_router.router)
    test_app.dependency_overrides[get_current_user] = lambda: None
    return TestClient(test_app)


@pytest.fixture
def installs(tmp_path, monkeypatch):
    path = tmp_path / "app_installs.json"
    monkeypatch.setattr(app_installs, "INSTALLS_PATH", path)
    app_installs._reset_for_tests()
    yield path
    app_installs._reset_for_tests()


def _disk(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_older_build_without_a_code_is_ignored(installs):
    app_installs.record("pixel", "Pixel", None, None, now=100.0)
    assert app_installs.installed(now=100.0) == []
    assert not installs.exists()


def test_new_device_is_written_and_heartbeats_are_throttled(installs):
    app_installs.record("pixel", "Pixel", "1.0.83", 183, now=100.0)
    assert _disk(installs)["pixel"]["last_seen_at"] == 100.0
    app_installs.record("pixel", "Pixel", "1.0.83", 183, now=200.0)
    assert _disk(installs)["pixel"]["last_seen_at"] == 100.0  # not rewritten yet
    assert app_installs.installed(now=250.0)[0]["last_seen_age_seconds"] == 50.0  # memory is fresher
    app_installs.record("pixel", "Pixel", "1.0.83", 183, now=100.0 + app_installs.REFRESH_SECONDS)
    assert _disk(installs)["pixel"]["last_seen_at"] == 100.0 + app_installs.REFRESH_SECONDS


def test_version_change_writes_now_and_resets_first_seen(installs):
    app_installs.record("pixel", "Pixel", "1.0.83", 183, now=100.0)
    app_installs.record("pixel", "Pixel", "1.0.84", 184, now=130.0)
    row = _disk(installs)["pixel"]
    assert (row["version_name"], row["version_code"], row["first_seen_at"]) == ("1.0.84", 184, 130.0)


def test_survives_a_restart(installs):
    app_installs.record("pixel", "Pixel", "1.0.83", 183, now=100.0)
    app_installs._reset_for_tests()
    assert app_installs.installed(now=100.0)[0]["version_name"] == "1.0.83"


def test_disk_error_never_raises(installs, monkeypatch):
    monkeypatch.setattr(app_installs, "INSTALLS_PATH", installs.parent / "nul\0bad" / "x.json")
    app_installs._reset_for_tests()
    app_installs.record("pixel", "Pixel", "1.0.83", 183, now=100.0)  # must not raise
    assert app_installs.installed(now=100.0) == []  # unreadable store: record nothing, overwrite nothing


def test_state_post_records_the_build(installs):
    from audiplex.playback_bus import PlaybackBus

    PlaybackBus().set_state({"playing": False, "app_version_name": "1.0.83", "app_version_code": 183}, "pixel")
    assert app_installs.installed()[0]["device_id"] == "pixel"


def test_installed_route_compares_against_the_served_apk(client, installs, tmp_path, monkeypatch):
    apk_dir = tmp_path / "apk"
    apk_dir.mkdir()
    (apk_dir / "output-metadata.json").write_text(json.dumps(
        {"elements": [{"versionCode": 184, "versionName": "1.0.84", "outputFile": "app.apk"}]}))
    monkeypatch.setattr(get_settings(), "apk_output_dir", str(apk_dir))
    app_installs.record("pixel", "Pixel", "1.0.83", 183)

    body = client.get("/api/app/installed").json()
    assert body["latest"] == {"versionName": "1.0.84", "versionCode": 184}
    assert body["installed"][0]["version_name"] == "1.0.83"
    assert body["installed"][0]["up_to_date"] is False


def test_installed_route_without_apk_metadata_still_lists_phones(client, installs, tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "apk_output_dir", str(tmp_path))
    app_installs.record("pixel", "Pixel", "1.0.83", 183)
    body = client.get("/api/app/installed").json()
    assert body["latest"] is None and body["installed"][0]["version_code"] == 183


def test_one_bad_row_keeps_the_other_phones(installs):
    installs.write_text(json.dumps({
        "pixel": {"device_id": "pixel", "version_name": "1.0.83", "version_code": 183,
                  "first_seen_at": 1.0, "last_seen_at": 2.0},
        "tab": {"device_id": "tab", "version_code": 180},  # no timestamps
    }), encoding="utf-8")
    app_installs.record("watch", "Watch", "1.0.83", 183, now=10.0)
    assert set(_disk(installs)) == {"pixel", "watch"}
    assert _disk(installs)["pixel"]["first_seen_at"] == 1.0


def test_unreadable_file_is_never_overwritten(installs):
    installs.write_text("{not json", encoding="utf-8")
    app_installs.record("pixel", "Pixel", "1.0.83", 183, now=10.0)
    assert installs.read_text(encoding="utf-8") == "{not json"
