"""#3493: agent-started DJ sound refuses under Pantheon quiet time / phone DND.

The Audiplex half of the #5971 mute audit. One gate (_mute_hold, run first in
_talk_hold) for every command that can make sound: it reads Pantheon's GLOBAL
mutes only and fails closed. A genuinely absent Pantheon directory sends (no
Pantheon on this box); an import or read error refuses. Stop-type commands are
never gated, so music already playing is never stopped.
"""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from audiplex_mcp import server as mcp_server  # noqa: E402

CONTRACT = ("Nothing was sent: Todd is in quiet time/DND ({why}). If he asked for "
            "this, offer to end quiet time, then retry. Do not tell him it is playing.")


def _pantheon(tmp_path, monkeypatch, body):
    """A fake Pantheon src dir whose device_mute_tell.py is `body`."""
    src = tmp_path / "pantheon_src"
    src.mkdir()
    (src / "device_mute_tell.py").write_text(body, encoding="utf-8")
    monkeypatch.setenv("DJ_PANTHEON_SRC", str(src))
    monkeypatch.setattr(sys, "path", list(sys.path))  # the gate appends; undo it
    monkeypatch.delitem(sys.modules, "device_mute_tell", raising=False)


def _mute(tmp_path, monkeypatch, muted=True, why="quiet window: test"):
    _pantheon(tmp_path, monkeypatch,
              "def global_mute_active(fail_closed=True):\n"
              f"    return {muted!r}, {why!r}\n")


@pytest.fixture
def wire(monkeypatch, tmp_path):
    sent = []
    st = {"state": {"playing": True, "queue": [{"id": 5, "index": 0}], "track": {"id": 5}}}

    async def fake_get(path):
        if path.startswith("/api/playback/commands"):
            return []
        if path.startswith("/api/music/tracks/"):
            return {"title": "Song", "artist_name": "Band"}
        if path.startswith("/api/playback/client-log"):
            return []
        if path == "/api/library/books":
            return [{"id": 9, "title": "Dune", "author": "Herbert"}]
        if path.startswith("/api/playback/devices"):
            return {"devices": [{"id": "phone", "name": "Pixel"}]}
        return st["state"]

    async def fake_post(path, body):
        if path == "/api/playback/tracks/playable":
            return {"playable": body["track_ids"], "missing": []}
        sent.append(("POST " + path, body))
        return {}

    async def fake_raw(cmd_type, payload):
        sent.append((cmd_type, payload))
        return {"id": len(sent), "pending": 1}

    async def no_gate(cmd_type):
        return None

    monkeypatch.setattr(mcp_server, "_get", fake_get)
    monkeypatch.setattr(mcp_server, "_post", fake_post)
    monkeypatch.setattr(mcp_server, "_enqueue_raw", fake_raw)
    monkeypatch.setattr(mcp_server, "_announce_gate", no_gate)
    monkeypatch.setattr(mcp_server, "ACK_WAIT_S", 0.0)
    monkeypatch.setenv("DJ_SPEECH_STATE_FILE", str(tmp_path / "no-speech.json"))
    return sent, st


@pytest.mark.parametrize("cmd", sorted(mcp_server.MUTE_GUARDED))
def test_every_sound_command_refused_when_muted(wire, tmp_path, monkeypatch, cmd):
    sent, _ = wire
    _mute(tmp_path, monkeypatch)
    out = asyncio.run(mcp_server._enqueue(cmd, {"track_ids": [1]} if cmd in ("play_now", "queue", "play_next") else {}))
    assert isinstance(out, str) and out.startswith("REFUSED (muted): ")
    assert CONTRACT.format(why="quiet window: test") in out
    assert sent == []


def test_gated_set_covers_announce_and_bed():
    assert {"play_now", "resume", "play_stream", "queue", "play_next", "activate",
            "play_book", "bed_play", "announce"} <= mcp_server.MUTE_GUARDED


@pytest.mark.parametrize("tool,args", [
    ("dj_play_now", ([1, 2],)),
    ("dj_queue", ([1],)),
    ("dj_play_next", ([1],)),
    ("dj_resume", ()),
    ("dj_play_stream", ("http://x/stream.mp3",)),
    ("dj_play_book", ("Dune",)),
    ("dj_bed_play", ("http://x/rain.mp3",)),
    ("dj_announce", ("Hello there",)),
    ("dj_transfer", ("phone",)),
])
def test_tools_refuse_and_send_nothing(wire, tmp_path, monkeypatch, tool, args):
    sent, _ = wire
    _mute(tmp_path, monkeypatch, why="device abc muted via phone DND")

    async def no_render(text, title):
        raise AssertionError("no TTS render under mute")

    monkeypatch.setattr(mcp_server, "_render_clip", no_render)
    out = asyncio.run(getattr(mcp_server, tool)(*args))
    assert "REFUSED (muted): Nothing was sent" in out, out
    assert "Do not tell him it is playing." in out
    assert sent == []


def test_refill_refusal_says_the_set_will_run_dry(wire, tmp_path, monkeypatch):
    _mute(tmp_path, monkeypatch)
    out = asyncio.run(mcp_server.dj_queue([1]))
    assert "will stop when its current queue runs out" in out
    assert "held=muted" in out


def test_pool_that_would_start_is_refused_before_saving(wire, tmp_path, monkeypatch):
    sent, st = wire
    st["state"] = {"playing": False, "queue": [], "track": {}}
    _mute(tmp_path, monkeypatch)

    async def fake_lanes(sources):
        return [{"name": "a", "track_ids": [1, 2]}], []

    monkeypatch.setattr(mcp_server, "_resolve_lanes", fake_lanes)
    monkeypatch.setattr(mcp_server, "_counts", lambda lanes: "a=2")
    out = asyncio.run(mcp_server.dj_pool_set(sources=[{"kind": "artist", "query": "x"}]))
    assert "held=muted" in out and "Nothing was sent" in out, out
    assert sent == []


@pytest.mark.parametrize("tool,args,cmd", [
    ("dj_pause", (), "pause"),
    ("dj_skip", (), "skip"),
    ("dj_volume", (40,), "volume"),
    ("dj_seek", (30,), "seek"),
    ("dj_bed_stop", (), "bed_stop"),
])
def test_stop_type_commands_pass_when_muted(wire, tmp_path, monkeypatch, tool, args, cmd):
    sent, _ = wire
    _mute(tmp_path, monkeypatch)
    out = asyncio.run(getattr(mcp_server, tool)(*args))
    assert "REFUSED" not in out, out
    assert [c for c, _ in sent] == [cmd]


def test_unmuted_sends(wire, tmp_path, monkeypatch):
    sent, _ = wire
    _mute(tmp_path, monkeypatch, muted=False, why="sound on")
    out = asyncio.run(mcp_server._enqueue("play_now", {"track_ids": [1]}))
    assert isinstance(out, dict)
    assert sent[0][0] == "play_now"


def test_read_error_fails_closed(wire, tmp_path, monkeypatch):
    sent, _ = wire
    _pantheon(tmp_path, monkeypatch,
              "def global_mute_active(fail_closed=True):\n    raise RuntimeError('db locked')\n")
    out = asyncio.run(mcp_server._enqueue("resume", {}))
    assert out.startswith("REFUSED (muted)") and "failing closed" in out
    assert sent == []


def test_import_error_fails_closed(wire, tmp_path, monkeypatch):
    """Karen rider (b): the directory is there but the module won't import."""
    sent, _ = wire
    _pantheon(tmp_path, monkeypatch, "import no_such_pantheon_module\n")
    out = asyncio.run(mcp_server._enqueue("play_now", {"track_ids": [1]}))
    assert out.startswith("REFUSED (muted)") and "ModuleNotFoundError" in out
    assert sent == []


def test_absent_pantheon_dir_sends(wire, tmp_path, monkeypatch, capsys):
    sent, _ = wire
    monkeypatch.setenv("DJ_PANTHEON_SRC", str(tmp_path / "nope"))
    out = asyncio.run(mcp_server._enqueue("play_now", {"track_ids": [1]}))
    assert isinstance(out, dict) and sent[0][0] == "play_now"
    assert "[mute guard] no Pantheon" in capsys.readouterr().err


def test_default_src_is_pantheon(monkeypatch):
    monkeypatch.delenv("DJ_PANTHEON_SRC", raising=False)
    assert mcp_server._pantheon_src() == Path("Q:/Pantheon/src")
