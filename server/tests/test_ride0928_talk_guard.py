"""#ride0928: music never starts while Todd is talking.

One guard (_todd_talking) for every start-type command, reading Pantheon's
speech state by default. A missing file means no Pantheon here (sent, with a
warning); a present-but-broken file holds (fail closed). Every start-type tool
leads with one RESULT line: sent / held / skipped_missing / phone_ack.
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from audiplex_mcp import server as mcp_server  # noqa: E402


@pytest.fixture
def wire(monkeypatch, tmp_path):
    sent = []
    st = {"state": {"playing": True, "queue": [{"id": 5, "index": 0}], "track": {"id": 5}},
          "missing": [], "acks": {}}

    async def fake_get(path):
        if path.startswith("/api/playback/commands"):
            return [{"id": i, "ack_status": s, "ack_detail": d} for i, (s, d) in st["acks"].items()]
        if path.startswith("/api/music/tracks/"):
            return {"title": f"Song {path.rsplit('/', 1)[1]}", "artist_name": "Band"}
        if path.startswith("/api/playback/client-log"):
            return []
        return st["state"]

    async def fake_post(path, body):
        if path == "/api/playback/tracks/playable":
            ids = body["track_ids"]
            return {"playable": [i for i in ids if i not in st["missing"]],
                    "missing": [i for i in ids if i in st["missing"]]}
        return {}

    async def fake_raw(cmd_type, payload):
        sent.append((cmd_type, payload))
        return {"id": len(sent), "pending": 1}

    monkeypatch.setattr(mcp_server, "_get", fake_get)
    monkeypatch.setattr(mcp_server, "_post", fake_post)
    monkeypatch.setattr(mcp_server, "_enqueue_raw", fake_raw)
    monkeypatch.setenv("DJ_PANTHEON_DB", str(tmp_path / "absent.db"))
    speech = tmp_path / "speech.json"
    monkeypatch.setenv("DJ_SPEECH_STATE_FILE", str(speech))
    return sent, st, speech


def _talk(speech, **flags):
    speech.write_text(json.dumps({"stt_active": False, "talk_active": False, "composing": False, **flags}))


def test_verdicts(wire):
    _, _, speech = wire
    assert mcp_server._todd_talking() == "missing"
    _talk(speech)
    assert mcp_server._todd_talking() == "clear"
    for key in ("stt_active", "talk_active", "composing"):
        _talk(speech, **{key: True})
        assert mcp_server._todd_talking() == "talking"
    _talk(speech, current_claim_holder="claude")
    assert mcp_server._todd_talking() == "clear"
    assert mcp_server._todd_talking(claim=True) == "talking"
    speech.write_text("{not json")
    assert mcp_server._todd_talking() == "unreadable"


def test_default_path_is_pantheon(monkeypatch):
    monkeypatch.delenv("DJ_SPEECH_STATE_FILE", raising=False)
    assert mcp_server._speech_state_path() == Path("Q:/Pantheon/data/runtime/speech_state.json")


@pytest.mark.parametrize("tool,args", [
    ("dj_play_now", ([1, 2],)),
    ("dj_queue", ([1],)),
    ("dj_play_next", ([1],)),
    ("dj_resume", ()),
    ("dj_play_stream", ("http://x/stream.mp3",)),
    ("dj_queue_by", ("x", "favorites")),
])
def test_start_tools_hold_while_talking(wire, tool, args, monkeypatch):
    sent, _, speech = wire
    _talk(speech, talk_active=True)

    async def fake_resolve(kind, query, recursive=True):
        return "favorites", [{"id": 1}]

    monkeypatch.setattr(mcp_server, "_resolve_source", fake_resolve)
    out = asyncio.run(getattr(mcp_server, tool)(*args))
    assert out.startswith("RESULT sent=0 held=todd_talking"), out
    assert sent == []


def test_unreadable_state_holds(wire):
    sent, _, speech = wire
    speech.write_text("")
    out = asyncio.run(mcp_server.dj_play_now([1]))
    assert "held=todd_talking" in out and "can't be read" in out
    assert sent == []


def test_missing_state_sends(wire):
    sent, st, _ = wire
    st["acks"] = {1: ("ok", None)}
    out = asyncio.run(mcp_server.dj_play_now([1]))
    assert out.startswith("RESULT sent=1 held=no skipped_missing=[] phone_ack=ok"), out
    assert sent[0][0] == "play_now"


def test_non_start_commands_pass_while_talking(wire):
    sent, _, speech = wire
    _talk(speech, talk_active=True)
    assert isinstance(asyncio.run(mcp_server._enqueue("pause", {})), dict)
    assert "skip was sent" in asyncio.run(mcp_server.dj_skip())  # #3505: unacked = NOT DONE
    assert [c for c, _ in sent] == ["pause", "skip"]


def test_transfer_holds_while_talking(wire, monkeypatch):
    sent, _, speech = wire
    _talk(speech, composing=True)

    async def fake_get(path):
        return {"devices": [{"id": "phone", "name": "Pixel"}]}

    async def boom(path, body):
        raise AssertionError("activate must not be sent")

    monkeypatch.setattr(mcp_server, "_get", fake_get)
    monkeypatch.setattr(mcp_server, "_post", boom)
    assert asyncio.run(mcp_server.dj_transfer("phone")).startswith("HELD (todd_talking)")


def test_result_names_skipped_titles_and_phone_ack(wire):
    sent, st, speech = wire
    _talk(speech)
    st["missing"] = [2]
    st["acks"] = {1: ("failed", "dropped 1 unresolvable id(s): [9]")}
    out = asyncio.run(mcp_server.dj_queue([1, 2]))
    first = out.splitlines()[0]
    assert first.startswith("RESULT sent=1 held=no")
    assert '"Song 2 - Band"' in first
    assert "phone_ack=failed: dropped 1 unresolvable" in first
    assert sent == [("queue", {"track_ids": [1]})]


def test_talk_cleared_then_start_goes_through(wire):
    sent, _, speech = wire
    _talk(speech, stt_active=True)
    assert "held=todd_talking" in asyncio.run(mcp_server.dj_resume())
    _talk(speech)
    assert asyncio.run(mcp_server.dj_resume()).startswith("RESULT sent=0 held=no")
    assert sent == [("resume", {})]
