"""#5457: every path default outside the repo comes from one machine-local root (audiplex_paths).

No machine path is written here (the remote is public): roots are tmp dirs, and the one test that
reads this box's real local_paths.json skips when the file is absent.
"""
import importlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import audiplex_paths  # noqa: E402
from audiplex import dj_triggers  # noqa: E402
from audiplex.music_video import worker  # noqa: E402
from audiplex_mcp import dj_bridge_watcher, playlist_fetch, song_context  # noqa: E402
from audiplex_mcp import server as mcp_server  # noqa: E402

REAL_LOCAL_FILE = audiplex_paths.REPO_ROOT / "local_paths.json"  # read before conftest patches LOCAL_FILE
ROOT_VARS = list(audiplex_paths.ROOTS.values())
SITE_VARS = ["DJ_PANTHEON_DB", "DJ_PANTHEON_SRC", "DJ_SPEECH_STATE_FILE", "DJ_BRIDGE_CMD", "RFL_DJ_NOTES_DB",
             "RFL_STORE_PY", "AUDIPLEX_COMFY_WORKFLOWS", "AUDIPLEX_PANTHEON_SRC", "DJ_YT_PLAYLISTS_FILE"]


@pytest.fixture
def clean(monkeypatch, tmp_path):
    for var in ROOT_VARS + SITE_VARS:
        monkeypatch.delenv(var, raising=False)
    local = tmp_path / "absent" / "local_paths.json"  # absent unless a test writes it
    monkeypatch.setattr(audiplex_paths, "LOCAL_FILE", local)
    return local


def _defaults() -> dict:
    return {
        "pantheon_db": mcp_server._pantheon_db_path(),
        "pantheon_src": mcp_server._pantheon_src(),
        "mcp_speech": mcp_server._speech_state_path(),
        "triggers_speech": dj_triggers.speech_state_path(),
        "bridge_cmd": dj_bridge_watcher.bridge_cmd_template(),
        "rfl_db": song_context._rfl_notes_db_path(),
        "rfl_store": song_context._rfl_store_path(),
        "comfy_workflows": worker.comfy_workflows(),
        "comfy_output": worker.comfy_output(),
        "worker_src": worker.pantheon_src(),
    }


def _expected(pantheon: str, dev: str, comfy: str) -> dict:
    p, d = Path(pantheon), Path(dev)
    return {
        "pantheon_db": p / "data" / "pantheon.db",
        "pantheon_src": p / "src",
        "mcp_speech": p / "data" / "runtime" / "speech_state.json",
        "triggers_speech": p / "data" / "runtime" / "speech_state.json",
        "bridge_cmd": f"{pantheon}/.venv-omnivoice/Scripts/python.exe "
                      f"{pantheon}/scripts/dj_bridge_push.py --payload-file {{json_file}}",
        "rfl_db": d / "radio_free_luna" / "data" / "dj_notes.db",
        "rfl_store": d / "radio_free_luna" / "src" / "notes" / "store.py",
        "comfy_workflows": d / "comfy_workflows",
        "comfy_output": Path(comfy),
        "worker_src": p / "src",
    }


def _set_roots(monkeypatch, tmp_path) -> tuple[str, str, str]:
    roots = tuple((tmp_path / name).as_posix() for name in ("pan", "dev", "comfy"))
    for var, value in zip(("AUDIPLEX_PANTHEON_ROOT", "AUDIPLEX_DEV_ROOT", "AUDIPLEX_COMFY_OUTPUT"), roots):
        monkeypatch.setenv(var, value)
    return roots


def test_an_unset_root_raises_and_says_which_file_and_key(clean):
    with pytest.raises(audiplex_paths.LocalPathsError) as err:
        mcp_server._pantheon_db_path()
    msg = str(err.value)
    assert str(clean) in msg and "'pantheon_root'" in msg and "AUDIPLEX_PANTHEON_ROOT" in msg
    assert "local_paths.example.json" in msg


def test_env_roots_move_every_default(clean, monkeypatch, tmp_path):
    expected = _expected(*_set_roots(monkeypatch, tmp_path))
    assert _defaults() == expected


def test_script_defaults_follow_the_root(clean, monkeypatch, tmp_path):
    pantheon, _dev, _comfy = _set_roots(monkeypatch, tmp_path)
    energy = importlib.reload(importlib.import_module("measure_energy"))
    profile = importlib.reload(importlib.import_module("measure_profile"))
    assert energy.DEFAULT_BIKE_STATE == profile.DEFAULT_BIKE_STATE == f"{pantheon}/data/runtime/bike_dj_state.json"
    assert profile.DEFAULT_SPEECH_STATE == f"{pantheon}/data/runtime/speech_state.json"


def test_local_file_is_honoured_and_env_beats_it(clean, monkeypatch, tmp_path):
    clean.parent.mkdir(parents=True)
    clean.write_text(json.dumps({"pantheon_root": (tmp_path / "local-pan").as_posix(),
                                 "dev_root": (tmp_path / "local-dev").as_posix()}), encoding="utf-8")
    assert mcp_server._pantheon_db_path() == tmp_path / "local-pan" / "data" / "pantheon.db"
    assert song_context._rfl_notes_db_path() == tmp_path / "local-dev" / "radio_free_luna" / "data" / "dj_notes.db"
    monkeypatch.setenv("AUDIPLEX_PANTHEON_ROOT", (tmp_path / "env-pan").as_posix())
    assert mcp_server._pantheon_db_path() == tmp_path / "env-pan" / "data" / "pantheon.db"


def test_backslash_roots_come_out_forward_slashed(clean, monkeypatch):
    monkeypatch.setenv("AUDIPLEX_PANTHEON_ROOT", "\\\\host\\share\\Pantheon\\")
    assert audiplex_paths.pantheon("src") == "//host/share/Pantheon/src"


def test_a_per_site_env_var_still_beats_the_root(clean, monkeypatch, tmp_path):
    _set_roots(monkeypatch, tmp_path)
    monkeypatch.setenv("DJ_SPEECH_STATE_FILE", str(tmp_path / "s.json"))
    monkeypatch.setenv("DJ_BRIDGE_CMD", "echo {json_file}")
    assert dj_triggers.speech_state_path() == tmp_path / "s.json"
    assert mcp_server._speech_state_path() == tmp_path / "s.json"
    assert dj_bridge_watcher.bridge_cmd_template() == "echo {json_file}"


@pytest.mark.parametrize("body", ["{not json", "[1, 2]", '{"pantheon_rot": "/x"}', '{"dev_root": ""}',
                                  '{"dev_root": 7}'])
def test_a_malformed_local_file_fails_loudly(clean, body):
    clean.parent.mkdir(parents=True)
    clean.write_text(body, encoding="utf-8")
    with pytest.raises(audiplex_paths.LocalPathsError):
        mcp_server._pantheon_db_path()


def test_playlist_dest_expands_music_root_and_absolute_still_works(clean, monkeypatch, tmp_path):
    music = (tmp_path / "music").as_posix()
    monkeypatch.setenv("AUDIPLEX_MUSIC_ROOT", music)
    allow = tmp_path / "yt_playlists.json"
    allow.write_text(json.dumps({"playlists": [
        {"id": "PL1", "name": "music", "dest": "{music_root}/Todd Walker - music"},
        {"id": "PL2", "name": "abs", "dest": "/fixed/dir"}]}), encoding="utf-8")
    monkeypatch.setenv("DJ_YT_PLAYLISTS_FILE", str(allow))
    got = {p["id"]: p["dest"] for p in playlist_fetch._load_allowlist()}
    assert got == {"PL1": f"{music}/Todd Walker - music", "PL2": "/fixed/dir"}


def test_the_shipped_allowlist_uses_the_music_root(clean, monkeypatch, tmp_path):
    music = (tmp_path / "music").as_posix()
    monkeypatch.setenv("AUDIPLEX_MUSIC_ROOT", music)
    assert playlist_fetch._load_allowlist()[0]["dest"] == f"{music}/Todd Walker - music"


@pytest.mark.skipif(not REAL_LOCAL_FILE.exists(), reason="this box has no local_paths.json")
def test_this_boxs_local_file_yields_every_default(clean, monkeypatch):
    """Local-only: with the real local_paths.json and no env, every default sits under its root."""
    monkeypatch.setattr(audiplex_paths, "LOCAL_FILE", REAL_LOCAL_FILE)
    real = json.loads(REAL_LOCAL_FILE.read_text(encoding="utf-8"))
    pantheon = real["pantheon_root"].replace("\\", "/").rstrip("/")
    dev = real["dev_root"].replace("\\", "/").rstrip("/")
    assert _defaults() == _expected(pantheon, dev, real["comfy_output"])
