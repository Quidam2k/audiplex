"""#4057: dj_skip marks a skip Todd asked for, so ride learning counts it as his."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from audiplex_mcp import server as mcp_server  # noqa: E402


def test_dj_skip_marks_todd_asked(monkeypatch):
    sent = []

    async def fake_enqueue(type_, payload):
        sent.append((type_, payload))
        return {"id": 1}

    async def fake_acked(data, label):
        return "ok"

    monkeypatch.setattr(mcp_server, "_enqueue", fake_enqueue)
    monkeypatch.setattr(mcp_server, "_acked", fake_acked)
    asyncio.run(mcp_server.dj_skip())
    asyncio.run(mcp_server.dj_skip(todd_asked=True))
    assert sent == [("skip", {}), ("skip", {"by": "todd"})]


def test_music_start_pauses_a_playing_book_first(monkeypatch):
    """#4052: play_now/play_stream bookmark a playing book before music replaces it."""
    calls = []

    async def fake_post(path, body):
        calls.append(path)
        return {"paused": True, "book": {"title": "Dune"}, "chapter_index": 2}

    monkeypatch.setattr(mcp_server, "_post", fake_post)
    got = asyncio.run(mcp_server._pause_book_first("play_now"))
    assert calls == ["/api/playback/pause-book"] and got["paused"]
    assert asyncio.run(mcp_server._pause_book_first("queue")) is None and len(calls) == 1
    assert "Dune, chapter 3" in mcp_server._guard_text({"book_paused": got})

    async def boom(path, body):
        raise RuntimeError("down")

    monkeypatch.setattr(mcp_server, "_post", boom)
    assert asyncio.run(mcp_server._pause_book_first("play_stream")) is None
