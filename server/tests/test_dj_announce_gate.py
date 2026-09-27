"""dj_announce's speech gate (#2858): no break is queued while Todd is talking."""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from audiplex_mcp import server as mcp_server  # noqa: E402
from audiplex_mcp import tts_backend  # noqa: E402


def _setup(monkeypatch, tmp_path, state):
    f = tmp_path / "speech_state.json"
    f.write_text(json.dumps(state))
    monkeypatch.setenv("DJ_SPEECH_STATE_FILE", str(f))
    monkeypatch.setattr(mcp_server, "SPEECH_GATE_WAIT_SECONDS", 2)

    async def no_sleep(_):
        return None

    monkeypatch.setattr(mcp_server.asyncio, "sleep", no_sleep)
    calls = []

    async def fake_synth(text):
        calls.append(text)
        raise tts_backend.TtsNotConfigured("stub")

    monkeypatch.setattr(tts_backend, "synthesize", fake_synth)
    return calls


def test_talking_blocks_the_break(monkeypatch, tmp_path):
    calls = _setup(monkeypatch, tmp_path, {"stt_active": False, "talk_active": True})
    out = asyncio.run(mcp_server.dj_announce("hello"))
    assert "Todd is talking" in out
    assert calls == []


def test_quiet_goes_through(monkeypatch, tmp_path):
    calls = _setup(monkeypatch, tmp_path, {"stt_active": False, "talk_active": False, "composing": False})
    out = asyncio.run(mcp_server.dj_announce("hello"))
    assert "TTS is not configured" in out
    assert calls == ["hello"]


def test_unset_or_unreadable_is_not_busy(monkeypatch, tmp_path):
    monkeypatch.delenv("DJ_SPEECH_STATE_FILE", raising=False)
    assert mcp_server._someone_talking() is False
    monkeypatch.setenv("DJ_SPEECH_STATE_FILE", str(tmp_path / "missing.json"))
    assert mcp_server._someone_talking() is False
