"""#3917: dj_announce falls back to the pre-rendered DJ bank when live TTS fails.

Gate order: mute hold (#3493) and the someone-talking wait (#2858) run BEFORE any
render or fallback. Uploads and enqueues are stubbed, so nothing reaches a device.
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from audiplex_mcp import dj_clip_bank  # noqa: E402
from audiplex_mcp import server as mcp_server  # noqa: E402
from audiplex_mcp import tts_backend  # noqa: E402


def _manifest(tmp_path, voice="feynman", extra=None):
    clips = {}
    for cid in ("dj_next", "dj_back", "that_was_generic", "that_was_cat_stevens", "track_title_generic"):
        wav = tmp_path / f"{cid}-{voice}.wav"
        wav.write_bytes(b"RIFF")
        clips[cid] = {"text": cid, "path": str(wav)}
    clips["dj_more"] = {"text": "gone", "path": str(tmp_path / "missing.wav")}
    m = {"version": 1, "voices": {voice: clips}, "artist_ids": {"Cat Stevens": "that_was_cat_stevens"}}
    m.update(extra or {})
    path = tmp_path / "dj_manifest.json"
    path.write_text(json.dumps(m))
    return path


@pytest.fixture
def rig(monkeypatch, tmp_path):
    """Voice configured, render failing, manifest present, no talk, no mute."""
    monkeypatch.setenv("DJ_TTS_VOICE", "feynman")
    monkeypatch.setenv("DJ_CLIP_MANIFEST", str(_manifest(tmp_path)))
    monkeypatch.setattr(mcp_server, "_mute_hold", lambda cmd: None)
    monkeypatch.setattr(mcp_server, "_someone_talking", lambda: False)
    monkeypatch.setattr(mcp_server, "SPEECH_GATE_WAIT_SECONDS", 2)

    async def no_sleep(_):
        return None

    monkeypatch.setattr(mcp_server.asyncio, "sleep", no_sleep)
    calls = {"synth": [], "upload": [], "enqueue": []}

    async def failing_synth(text):
        calls["synth"].append(text)
        raise tts_backend.TtsFailed("DJ_TTS_CMD exited 1: Athena render failed")

    async def fake_upload(path, title, delete):
        calls["upload"].append((Path(path), delete))
        return {"clip_id": 77, "url": "/api/dj/clips/77", "duration_seconds": 1.5}

    async def fake_enqueue(cmd_type, payload):
        calls["enqueue"].append((cmd_type, payload))
        return {"id": 9, "pending": 1}

    monkeypatch.setattr(tts_backend, "synthesize", failing_synth)
    monkeypatch.setattr(mcp_server, "_upload_clip", fake_upload)
    monkeypatch.setattr(mcp_server, "_enqueue", fake_enqueue)
    return calls


def test_failed_render_enqueues_the_bank_clip(rig, tmp_path):
    out = asyncio.run(mcp_server.dj_announce("Here comes a long one"))
    assert "(pre-rendered fallback: dj_next, Athena voice unavailable)" in out, out
    assert rig["synth"] == ["Here comes a long one"]
    (path, delete), = rig["upload"]
    assert path.name == "dj_next-feynman.wav" and delete is False
    assert path.exists()  # the bank file is never deleted
    (cmd, payload), = rig["enqueue"]
    assert cmd == "announce" and payload["clip_id"] == 77


def test_that_was_uses_the_artist_clip_or_the_generic(rig):
    asyncio.run(mcp_server.dj_announce("x", fallback_kind="that_was", artist="Cat Stevens"))
    asyncio.run(mcp_server.dj_announce("x", fallback_kind="that_was", artist="Nobody"))
    assert [p.name for p, _ in rig["upload"]] == [
        "that_was_cat_stevens-feynman.wav", "that_was_generic-feynman.wav"]


def test_voice_unset_keeps_todays_failure(rig, monkeypatch):
    monkeypatch.delenv("DJ_TTS_VOICE")
    out = asyncio.run(mcp_server.dj_announce("hello"))
    assert out.startswith("Speech synthesis failed:"), out
    assert rig["upload"] == [] and rig["enqueue"] == []


def test_not_configured_never_falls_back(rig, monkeypatch):
    async def unconfigured(text):
        raise tts_backend.TtsNotConfigured("stub")

    monkeypatch.setattr(tts_backend, "synthesize", unconfigured)
    out = asyncio.run(mcp_server.dj_announce("hello"))
    assert "TTS is not configured" in out
    assert rig["upload"] == [] and rig["enqueue"] == []


def test_bank_miss_keeps_todays_failure(rig):
    out = asyncio.run(mcp_server.dj_announce("hello", fallback_kind="dj_more"))  # file missing
    assert out.startswith("Speech synthesis failed:"), out
    out = asyncio.run(mcp_server.dj_announce("hello", fallback_kind="no_such_line"))
    assert out.startswith("Speech synthesis failed:"), out
    assert rig["enqueue"] == []


def test_unreadable_manifest_keeps_todays_failure(rig, monkeypatch, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    monkeypatch.setenv("DJ_CLIP_MANIFEST", str(bad))
    assert asyncio.run(mcp_server.dj_announce("hello")).startswith("Speech synthesis failed:")
    assert rig["enqueue"] == []


def test_muted_means_no_render_and_no_clip(rig, monkeypatch):
    monkeypatch.setattr(mcp_server, "_mute_hold", lambda cmd: "REFUSED (muted): Nothing was sent")
    out = asyncio.run(mcp_server.dj_announce("hello"))
    assert out.startswith("REFUSED (muted)")
    assert rig["synth"] == [] and rig["upload"] == [] and rig["enqueue"] == []


def test_todd_talking_means_no_render_and_no_clip(rig, monkeypatch):
    monkeypatch.setattr(mcp_server, "_someone_talking", lambda: True)
    out = asyncio.run(mcp_server.dj_announce("hello"))
    assert "Todd is talking" in out
    assert rig["synth"] == [] and rig["upload"] == [] and rig["enqueue"] == []


@pytest.mark.parametrize("name,key", [("jarvis", "claude"), ("Karen", "gemini"), ("orolo", "orolo"),
                                      ("feynman", "feynman"), ("", None)])
def test_manifest_voice(monkeypatch, name, key):
    monkeypatch.setenv("DJ_TTS_VOICE", name)
    assert dj_clip_bank.manifest_voice() == key


def test_new_keyed_family_slots_in_from_the_manifest(tmp_path):
    """#3919: per-track title clips need only a "<kind>_ids" map in the manifest."""
    m = json.loads(_manifest(tmp_path, extra={"track_title_ids": {"42": "dj_back"}}).read_text())
    assert dj_clip_bank.lookup(m, "feynman", "track_title", "42")[0] == "dj_back"
    assert dj_clip_bank.lookup(m, "feynman", "track_title", "99")[0] == "track_title_generic"
    assert dj_clip_bank.lookup(m, "claude", "dj_next") is None  # voice not rendered yet
