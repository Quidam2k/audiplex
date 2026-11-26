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
    calls = _setup(monkeypatch, tmp_path, {"stt_active": True, "talk_active": True})  # #7028 live speech
    out = asyncio.run(mcp_server.dj_announce("hello"))
    assert "Todd is talking" in out
    assert calls == []


def test_talk_left_on_while_silent_is_not_talking(monkeypatch, tmp_path):  # #7028
    """#7028 2026-11-25 18:48 PT: Talk ON (lav), Todd silent between lines. The exact
    speech_state Pantheon wrote must read clear, for music starts AND swaps."""
    f = tmp_path / "speech_state.json"
    f.write_text(json.dumps({"stt_active": False, "stt_active_leg": None, "composing": False,
                             "talk_active": True, "current_claim_holder": None}))
    monkeypatch.setenv("DJ_SPEECH_STATE_FILE", str(f))
    assert mcp_server._todd_talking() == "clear"
    assert mcp_server._someone_talking() is False
    assert mcp_server._speech_busy() is False
    assert mcp_server._talk_hold("play") is None
    f.write_text(json.dumps({"stt_active": True, "talk_active": True}))  # he speaks: held again
    assert mcp_server._todd_talking() == "talking"


def test_quiet_goes_through(monkeypatch, tmp_path):
    calls = _setup(monkeypatch, tmp_path, {"stt_active": False, "talk_active": False, "composing": False})
    out = asyncio.run(mcp_server.dj_announce("hello"))
    assert "TTS is not configured" in out
    assert calls == ["hello"]


def test_unset_or_unreadable_is_not_busy(monkeypatch, tmp_path):
    monkeypatch.delenv("DJ_SPEECH_STATE_FILE", raising=False)
    # #4222: the unset fallback is Pantheon's REAL speech state; point it at a missing file.
    monkeypatch.setattr(mcp_server, "_default_speech_state_file", lambda: str(tmp_path / "default.json"))  # #5457
    assert mcp_server._someone_talking() is False
    monkeypatch.setenv("DJ_SPEECH_STATE_FILE", str(tmp_path / "missing.json"))
    assert mcp_server._someone_talking() is False
