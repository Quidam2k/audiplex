"""dj_mix MCP tool branches (#2842, #5463), with the HTTP layer stubbed out.

The planning itself is covered by test_mix.py and the /mix/plan endpoint test;
this pins what the tool SENDS: replace_upcoming after the current song, play_now
only when nothing is loaded, and on an old phone build (no replace_upcoming) the
WHOLE re-planned mix swapped in at a song boundary (#5463), never an append.
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from audiplex_mcp import server as mcp_server  # noqa: E402

PLAYING = {
    "playing": True,
    "track": {"id": 5, "title": "Now"},
    "queue_index": 1,
    "queue": [{"index": 0, "id": 4}, {"index": 1, "id": 5}, {"index": 2, "id": 6}],
}


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):  # #5463: never read Pantheon's live files
    monkeypatch.setenv("DJ_MIX_SOURCES_FILE", str(tmp_path / "sources.json"))
    monkeypatch.setenv("DJ_SPEECH_STATE_FILE", str(tmp_path / "speech.json"))
    monkeypatch.setattr(mcp_server, "SWAP_POLL_S", 0)
    mcp_server._SWAP.update(task=None, status="none", detail="", ids=[])
    return tmp_path


@pytest.fixture
def wire(monkeypatch):
    sent, posted = [], []
    calls = {"state": PLAYING, "ack": {"ack_status": "ok"}}

    async def fake_get(path):
        if path.startswith("/api/playback/commands"):
            return []
        assert path == "/api/playback/state"
        return calls["state"]

    async def fake_post(path, body):
        if path.endswith("/candidates/filter"):
            return {"allowed": body["track_ids"], "suppressed": []}
        posted.append(body)
        tail = body.get("upcoming_ids", []) + [i for i in body["new_ids"] if i not in body.get("upcoming_ids", [])]
        return {"upcoming": tail, "summary": "s"}

    async def fake_enqueue(cmd_type, payload):
        sent.append((cmd_type, payload["track_ids"]))
        return {"id": len(sent), "pending": 1}

    async def fake_ack(command_id, timeout=12.0):
        return calls["ack"]

    monkeypatch.setattr(mcp_server, "_get", fake_get)
    monkeypatch.setattr(mcp_server, "_post", fake_post)
    monkeypatch.setattr(mcp_server, "_enqueue", fake_enqueue)
    monkeypatch.setattr(mcp_server, "_await_ack", fake_ack)
    return sent, posted, calls


def run(**kw):
    return asyncio.run(mcp_server.dj_mix(**kw))


def test_playing_sends_replace_upcoming_and_spares_current(wire):
    sent, posted, _ = wire
    out = run(track_ids=[7, 8], shuffle=False)
    assert posted[0]["current_id"] == 5
    assert posted[0]["played_ids"] == [4]
    assert posted[0]["upcoming_ids"] == [6]
    assert sent == [("replace_upcoming", [6, 7, 8])]
    assert "after the current song" in out


def test_nothing_loaded_starts_with_play_now(wire):
    sent, posted, calls = wire
    calls["state"] = {"track": None, "queue": [], "queue_index": 0}
    run(track_ids=[7, 8], shuffle=False)
    assert "current_id" not in posted[0]
    assert sent == [("play_now", [7, 8])]


def test_old_phone_build_paused_sends_nothing(wire):  # #5463
    """Paused + no replace_upcoming: play_now would start music, so nothing goes."""
    sent, _, calls = wire
    calls["ack"] = {"ack_status": "unknown_type", "ack_detail": "replace_upcoming"}
    calls["state"] = {**PLAYING, "playing": False}
    out = run(track_ids=[6, 7], shuffle=False)
    assert sent == [("replace_upcoming", [6, 7])]  # the probe only; no append, no play_now
    assert "NOTHING was sent" in out


def test_live_stream_is_left_alone(wire):
    sent, _, calls = wire
    calls["state"] = {"track": {"id": -1}, "queue": [], "queue_index": 0}
    out = run(track_ids=[7])
    assert sent == []
    assert "live stream" in out


def test_track_ids_file(wire, tmp_path):
    sent, _, calls = wire
    calls["state"] = {"track": None, "queue": [], "queue_index": 0}
    f = tmp_path / "ids.json"
    f.write_text("[9, 10]", encoding="utf-8")
    run(track_ids_file=str(f), shuffle=False)
    assert sent == [("play_now", [9, 10])]


def test_voice_break_playing_is_not_interrupted(wire):
    """A DJ break (negative id) is the current item; a mix goes after it."""
    sent, posted, calls = wire
    calls["state"] = {
        "playing": True,
        "track": {"id": -3, "title": "DJ break"},
        "queue_index": 0,
        "queue": [{"index": 0, "id": -3}, {"index": 1, "id": 6}],
    }
    run(track_ids=[7], shuffle=False)
    assert posted[0]["current_id"] == -3
    assert sent[0][0] == "replace_upcoming"


def test_fully_trimmed_plan_still_clears_the_tail(wire, monkeypatch):
    """Everything still queued already played → the tail is emptied, not kept."""
    sent, _, _ = wire

    async def empty_plan(path, body):
        if path.endswith("/candidates/filter"):
            return {"suppressed": []}
        return {"upcoming": [], "summary": "s"}

    monkeypatch.setattr(mcp_server, "_post", empty_plan)
    run(track_ids=[4])
    assert sent == [("replace_upcoming", [])]


# ----- #5463: today's scenario on a fake OLD-APK phone -----

REN = list(range(1, 31))  # 30 "Ren Faire" tracks
YT = list(range(101, 109))  # 8 "YouTube" tracks
RECENT = {3, 104}  # heard in the last 12h


class OldPhone:
    """A phone build without replace_upcoming: play_now replaces, queue appends.

    Every state read advances the current song by one second, so a swap that
    waits for the song boundary can be observed doing so.
    """

    def __init__(self):
        self.queue, self.index, self.pos, self.dur = [500, 501], 0, 0, 10_000
        self.sent, self.pos_when_sent, self.talking = [], [], False

    def state(self):
        self.pos += 1000
        if self.pos >= self.dur and self.index + 1 < len(self.queue):
            self.index, self.pos = self.index + 1, 0
        return {
            "playing": True,
            "track": {"id": self.queue[self.index]},
            "position_ms": self.pos,
            "duration_ms": self.dur,
            "queue_index": self.index,
            "queue": [{"index": i, "id": t} for i, t in enumerate(self.queue)],
        }


@pytest.fixture
def phone(monkeypatch, isolated):
    dev = OldPhone()
    history = []

    async def fake_get(path):
        if path.startswith("/api/playback/commands"):
            return history
        return dev.state()

    async def fake_post(path, body):
        if path.endswith("/candidates/filter"):
            assert body["recording_cooldown_minutes"] == 12 * 60
            gone = [i for i in body["track_ids"] if i in RECENT]
            return {"suppressed": [{"track_id": i} for i in gone]}
        cur = body.get("current_id")
        pool = [i for i in body.get("upcoming_ids", []) + body["new_ids"] if i != cur]
        return {"upcoming": list(dict.fromkeys(pool)), "summary": "s"}

    async def fake_enqueue(cmd_type, payload):
        ids = payload["track_ids"]
        dev.sent.append((cmd_type, ids))
        dev.pos_when_sent.append(dev.pos)
        if cmd_type == "replace_upcoming":
            history.append({"type": "replace_upcoming", "ack_status": "unknown_type"})
        elif cmd_type == "play_now":
            dev.queue, dev.index, dev.pos = list(ids), 0, 0
        elif cmd_type == "queue":
            dev.queue += ids
        return {"id": len(dev.sent), "pending": 1}

    async def fake_ack(command_id, timeout=12.0):
        return {"ack_status": "unknown_type"}

    async def fake_resolve(kind, query, recursive=True):
        return query, [{"id": i} for i in (REN if "Ren" in query else YT)]

    monkeypatch.setattr(mcp_server, "_get", fake_get)
    monkeypatch.setattr(mcp_server, "_post", fake_post)
    monkeypatch.setattr(mcp_server, "_enqueue", fake_enqueue)
    monkeypatch.setattr(mcp_server, "_await_ack", fake_ack)
    monkeypatch.setattr(mcp_server, "_resolve_source", fake_resolve)
    return dev


def _alternates(ids):
    ren, yt = set(REN), set(YT)
    ids = [i for i in ids if i in ren or i in yt]  # the old tail (501) is its own source
    n_yt = len([i for i in ids if i in yt])
    head = ids[: 2 * n_yt]
    return all(len({a in ren, b in ren}) == 2 for a, b in zip(head[::2], head[1::2]))


async def _two_calls(dev, wait_between: bool):
    out1 = await mcp_server.dj_mix(sources=[{"kind": "folder", "query": "H:/Music/Ren Faire"}], seed=1)
    if wait_between:
        await mcp_server._SWAP["task"]
    out2 = await mcp_server.dj_mix(sources=[{"kind": "folder", "query": "H:/Music/YouTube"}], seed=2)
    await mcp_server._SWAP["task"]
    return out1, out2


@pytest.mark.parametrize("wait_between", [False, True])
def test_two_call_mix_on_old_phone_interleaves(phone, wait_between):
    """Today: dj_mix(Ren Faire) then dj_mix(YouTube) must NOT queue them as blocks."""
    out1, out2 = asyncio.run(_two_calls(phone, wait_between))
    assert "Old phone build" in out2 and "when the current song ends" in out2
    assert not [c for c in phone.sent if c[0] == "queue" and set(c[1]) & set(YT)], "no append-only fallback"
    final = phone.queue
    expected = (set(REN) | set(YT) | {501}) - RECENT  # 501: the old tail is kept, mixed in
    assert set(final) <= expected
    assert len(expected - set(final)) <= 1  # only the song that was playing at the swap
    assert _alternates(final), final[:16]
    assert mcp_server._SWAP["status"] == "fired"


def test_swap_waits_for_the_song_to_end(phone):
    asyncio.run(_two_calls(phone, False))
    plays = [p for (c, _), p in zip(phone.sent, phone.pos_when_sent) if c == "play_now"]
    assert len(plays) == 1, "one swap: the second mix superseded the first"
    assert plays[0] >= phone.dur - mcp_server.SWAP_NEAR_END_MS or plays[0] <= mcp_server.SWAP_START_GRACE_MS


def test_swap_defers_while_todd_talks(phone, isolated):  # #2845
    speech = isolated / "speech.json"
    speech.write_text(json.dumps({"talk_active": True}), encoding="utf-8")

    async def scenario():
        await mcp_server.dj_mix(sources=[{"kind": "folder", "query": "Ren"}, {"kind": "folder", "query": "YT"}])
        for _ in range(30):  # three songs' worth of polls, all while talking
            await asyncio.sleep(0)
        assert not [c for c in phone.sent if c[0] == "play_now"]
        speech.write_text(json.dumps({"talk_active": False}), encoding="utf-8")
        await mcp_server._SWAP["task"]

    asyncio.run(scenario())
    assert [c for c in phone.sent if c[0] == "play_now"]
    assert _alternates(phone.queue)


def test_one_call_two_sources_is_even_by_default(wire):
    sent, _, calls = wire
    calls["state"] = {"track": None, "queue": [], "queue_index": 0}

    async def fake_resolve(kind, query, recursive=True):
        return query, [{"id": i} for i in (REN if query == "Ren" else YT)]

    mcp_server._resolve_source, orig = fake_resolve, mcp_server._resolve_source
    try:
        run(sources=[{"kind": "folder", "query": "Ren"}, {"kind": "folder", "query": "YT"}], seed=4)
    finally:
        mcp_server._resolve_source = orig
    assert _alternates(sent[0][1])


# ----- #5473: per-source counts, zero-source refusal, loose files, folder_match -----

TREE = {  # folder listing by path (None = roots)
    None: {"folders": [{"path": "H:/Deck"}]},
    "H:/Deck": {"folders": [{"path": "H:/Deck/Ren Faire 1"}, {"path": "H:/Deck/Individual"}],
                "albums": [{"id": 90}]},
    "H:/Deck/Individual": {"folders": [{"path": "H:/Deck/Individual/faster"}], "albums": []},
}
FOLDER_TRACKS = {"H:/Deck/Ren Faire 1": [1, 2], "H:/Deck/Individual/faster": [], "H:/Deck": [1, 2, 7, 8]}


@pytest.fixture
def library(wire, monkeypatch):
    sent, posted, calls = wire
    calls["state"] = {"track": None, "queue": [], "queue_index": 0}
    base_get = mcp_server._get

    async def fake_get(path):
        from urllib.parse import unquote
        if path == "/api/music/folders":
            return TREE[None]
        if path.startswith("/api/music/folders?path="):
            p = unquote(path.split("=", 1)[1])
            if p not in TREE:
                raise mcp_server.httpx.HTTPStatusError("404", request=None, response=None)
            return TREE[p]
        if path.startswith("/api/music/folders/tracks?path="):
            return [{"id": i} for i in FOLDER_TRACKS.get(unquote(path.split("=", 1)[1]), [])]
        if path == "/api/music/albums/90":
            return {"tracks": [{"id": 7}, {"id": 8}]}
        return await base_get(path)

    monkeypatch.setattr(mcp_server, "_get", fake_get)
    return sent


def test_zero_track_source_refuses_and_names_it(library):
    out = run(sources=[{"kind": "folder", "query": "H:/Deck/Ren Faire 1"},
                       {"kind": "folder", "query": "H:/Deck/Individual/faster"}])
    assert library == []
    assert out.startswith("REFUSED") and "faster" in out and "(2)" in out


def test_allow_empty_mixes_without_the_empty_source(library):
    run(sources=[{"kind": "folder", "query": "H:/Deck/Ren Faire 1"},
                 {"kind": "folder", "query": "H:/Nope"}], allow_empty=True, shuffle=False)
    assert library == [("play_now", [1, 2])]


def test_loose_files_only_and_folder_match(library):
    out = run(sources=[{"kind": "folder", "query": "H:/Deck", "recursive": False},
                       {"kind": "folder_match", "query": "ren faire"}], shuffle=False)
    assert sorted(library[0][1]) == [1, 2, 7, 8]
    assert "loose files in 'H:/Deck' (2)" in out and "folders matching 'ren faire' (1 folder(s)) (2)" in out
