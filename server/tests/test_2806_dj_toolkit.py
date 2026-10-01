"""#2806: the DJ toolkit: queue edits, pool lanes, bans, search/tracks sources.

Queue edits compute the new tail in the MCP and send ONE replace_upcoming;
the current song is never touched. Bans are server-side and honored by mix
plans and pool picks across copies of a recording; dj_unban reverses them.
"""

import asyncio
import sys
from pathlib import Path

import pytest

from audiplex.dj_pool import get_pool
from audiplex.models import Track
from audiplex.routers import playback

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from audiplex_mcp import dj_toolkit, server as mcp  # noqa: E402


def run(coro):
    return asyncio.run(coro)


def _track(db, album, title):
    t = Track(title=title, album_id=album.id, artist_id=album.artist_id,
              file_path=f"/m/{title}.mp3", file_size=1, duration_seconds=200)
    db.add(t)
    db.commit()
    return t


def _owner_is_testuser(monkeypatch):
    s = playback.get_settings().model_copy(update={"dj_owner_username": "testuser"})
    monkeypatch.setattr(playback, "get_settings", lambda: s)


# ----- server: bans ------------------------------------------------------


def test_ban_list_unban_roundtrip(client, db_session, sample_album):
    a = _track(db_session, sample_album, "Alpha")
    r = client.post("/api/playback/bans", json={"track_ids": [a.id, 99999], "reason": "cheesy",
                                                 "persona": "orolo"}).json()
    assert r == {"banned": [a.id], "already": [], "unknown": [99999]}
    assert client.post("/api/playback/bans", json={"track_ids": [a.id]}).json()["already"] == [a.id]
    rows = client.get("/api/playback/bans").json()
    assert [(x["track_id"], x["title"], x["reason"], x["persona"]) for x in rows] == [
        (a.id, "Alpha", "cheesy", "orolo")]
    r = client.request("DELETE", "/api/playback/bans", json={"track_ids": [a.id, 5]}).json()
    assert r == {"unbanned": [a.id], "not_banned": [5]}
    assert client.get("/api/playback/bans").json() == []


def test_mix_plan_skips_banned(client, db_session, sample_album, monkeypatch):
    _owner_is_testuser(monkeypatch)
    a, b = _track(db_session, sample_album, "Alpha"), _track(db_session, sample_album, "Beta")
    client.post("/api/playback/bans", json={"track_ids": [b.id]})
    plan = client.post("/api/playback/mix/plan", json={"new_ids": [a.id, b.id], "shuffle": False}).json()
    assert plan["upcoming"] == [a.id]
    client.request("DELETE", "/api/playback/bans", json={"track_ids": [b.id]})
    plan = client.post("/api/playback/mix/plan", json={"new_ids": [a.id, b.id], "shuffle": False}).json()
    assert plan["upcoming"] == [a.id, b.id]


def test_ban_covers_other_copies_of_the_recording(db_session, sample_album):
    from audiplex.dj_bans import banned_ids
    from audiplex.models import Album, DjBan

    other = Album(title="Best Of", artist_id=sample_album.artist_id, folder_path="/m/copy")
    db_session.add(other)
    db_session.commit()
    a = _track(db_session, sample_album, "Same Song")
    b = Track(title="Same Song", album_id=other.id, artist_id=other.artist_id,
              file_path="/m/copy/Same Song.mp3", file_size=1, duration_seconds=200)
    db_session.add_all([b, DjBan(track_id=a.id)])
    db_session.commit()
    assert {a.id, b.id} <= banned_ids(db_session)


def test_pool_start_and_topup_skip_banned(client, db_session, sample_album):
    ids = [_track(db_session, sample_album, f"T{i}").id for i in range(6)]
    client.post("/api/playback/bans", json={"track_ids": [ids[0]]})
    r = client.post("/api/playback/pool", json={
        "lanes": {"mix": ids}, "balance": "even", "ahead": 2, "prime_current_id": 0}).json()
    assert ids[0] not in r["initial_picks"]
    # A ban made while the pool runs is honored by the next top-up.
    client.post("/api/playback/bans", json={"track_ids": [ids[3]]})
    top = get_pool().top_up(current_track_id=ids[1], upcoming_track_ids=[ids[1]], db=db_session)
    assert ids[3] not in top["picks"] and ids[0] not in top["picks"]


# ----- server: pool lanes --------------------------------------------------


def test_pool_lane_pause_resume_remove(client, db_session, sample_album):
    a = [_track(db_session, sample_album, f"A{i}").id for i in range(4)]
    b = [_track(db_session, sample_album, f"B{i}").id for i in range(4)]
    client.post("/api/playback/pool", json={"lanes": {"Rock": a, "Jazz": b}, "ahead": 3})
    r = client.patch("/api/playback/pool/lanes", json={"lane": "jaz", "action": "pause"}).json()
    assert r["lane"] == "Jazz"
    assert {x["label"]: x["paused"] for x in r["lanes"]} == {"Rock": False, "Jazz": True}
    top = get_pool().top_up(current_track_id=a[0], upcoming_track_ids=[a[0]], db=db_session)
    assert top["picks"] and not set(top["picks"]) & set(b)
    client.patch("/api/playback/pool/lanes", json={"lane": "Jazz", "action": "resume"})
    assert not get_pool().state["lanes"]["Jazz"]["paused"]
    r = client.patch("/api/playback/pool/lanes", json={"lane": "Rock", "action": "remove"}).json()
    assert [x["label"] for x in r["lanes"]] == ["Jazz"]


def test_pool_lane_errors_are_400_and_say_why(client):
    client.post("/api/playback/pool", json={"lanes": {"Rock": [1]}})
    r = client.patch("/api/playback/pool/lanes", json={"lane": "Polka", "action": "pause"})
    assert r.status_code == 400 and "Rock" in r.json()["detail"]
    r = client.patch("/api/playback/pool/lanes", json={"lane": "Rock", "action": "explode"})
    assert r.status_code == 400 and "pause" in r.json()["detail"]


def test_paused_lane_survives_a_spec_resync():
    pool = get_pool()
    pool.set_pool(None, [1, 2], {}, lanes={"Rock": {"track_ids": [1]}, "Jazz": {"track_ids": [2]}})
    pool.set_lane("Jazz", "pause")
    pool.resync_lanes({"Rock": [1], "Jazz": [2, 3]})
    assert pool.state["lanes"]["Jazz"]["paused"] is True


# ----- MCP: queue edits ------------------------------------------------------


def _q(*ids, start=0):
    return [{"index": start + i, "id": t, "title": f"t{t}", "artist": "a"} for i, t in enumerate(ids)]


@pytest.fixture
def dev(monkeypatch):
    d = {"state": {"playing": True, "track": {"id": 10}, "queue_index": 1,
                   "queue_length": 5, "queue": _q(9, 10, 11, 12, 13)},
         "sent": [], "ack": {"ack_status": "ok"}, "lacks": False, "posts": [], "patches": []}

    async def fake_get(path):
        if path.startswith("/api/playback/state"):
            return d["state"]
        if path.startswith("/api/playback/bans"):
            return [{"track_id": 11, "title": "t11", "artist": "a", "reason": "no", "persona": None}]
        return []

    async def fake_raw(cmd_type, payload):
        d["sent"].append((cmd_type, payload))
        return {"id": 5, "type": cmd_type, "pending": 1}

    async def fake_post(path, body):
        if path == "/api/playback/tracks/playable":
            return {"playable": body["track_ids"], "missing": []}
        d["posts"].append((path, body))
        return {"banned": body.get("track_ids"), "already": [], "unknown": []}

    async def fake_ack(cid, timeout=12.0):
        return d["ack"]

    async def lacks():
        return d["lacks"]

    async def no_gate(cmd_type):
        return None

    monkeypatch.setattr(mcp, "_get", fake_get)
    monkeypatch.setattr(mcp, "_post", fake_post)
    monkeypatch.setattr(mcp, "_enqueue_raw", fake_raw)
    monkeypatch.setattr(mcp, "_await_ack", fake_ack)
    monkeypatch.setattr(mcp, "_device_lacks_replace_upcoming", lacks)
    monkeypatch.setattr(mcp, "_announce_gate", no_gate)
    monkeypatch.setattr(mcp, "_load_source_map", lambda: {11: "Rock"})
    return d


def test_upcoming_lists_current_and_tail_with_sources(dev):
    out = run(dj_toolkit.dj_upcoming())
    assert "3 track(s) after" in out
    assert ">#1  id 10" in out and "#2  id 11  t11 - a  [Rock]" in out
    assert "id 9 " not in out  # already played


def test_remove_by_index_and_id_sends_one_replace_upcoming(dev):
    out = run(dj_toolkit.dj_remove(indexes=[2], track_ids=[13]))
    assert dev["sent"] == [("replace_upcoming", {"track_ids": [12]})]
    assert "Removing 2" in out and "Done" in out


def test_edits_never_touch_the_current_song(dev):
    for out in (run(dj_toolkit.dj_remove(indexes=[1])),
                run(dj_toolkit.dj_swap(0, [99])),
                run(dj_toolkit.dj_insert([99], at_index=1))):
        assert "playing now or already played" in out
    assert dev["sent"] == []


def test_insert_at_index_and_at_end(dev):
    run(dj_toolkit.dj_insert([77, 78], at_index=3))
    assert dev["sent"][-1] == ("replace_upcoming", {"track_ids": [11, 77, 78, 12, 13]})
    run(dj_toolkit.dj_insert([79]))
    assert dev["sent"][-1] == ("replace_upcoming", {"track_ids": [11, 12, 13, 79]})
    assert "ends at #4" in run(dj_toolkit.dj_insert([1], at_index=9))


def test_swap_replaces_one_slot(dev):
    run(dj_toolkit.dj_swap(3, [50, 51]))
    assert dev["sent"] == [("replace_upcoming", {"track_ids": [11, 50, 51, 13]})]


def test_refuses_with_clip_queued_truncated_or_book(dev):
    dev["state"]["queue"] = _q(9, 10, -1, 12, 13)
    assert "DJ clip" in run(dj_toolkit.dj_remove(indexes=[3]))
    dev["state"]["queue"] = _q(10, 11, start=1)  # PC-style window, but 5 queued
    dev["state"]["queue_length"] = 9
    assert "drop" in run(dj_toolkit.dj_remove(indexes=[2]))
    dev["state"]["book"] = {"id": 7}
    assert "audiobook" in run(dj_toolkit.dj_upcoming())
    assert dev["sent"] == []


def test_pc_style_windowed_queue_is_editable(dev):
    dev["state"]["queue"] = _q(10, 11, 12, 13, start=1)
    run(dj_toolkit.dj_remove(indexes=[2]))
    assert dev["sent"] == [("replace_upcoming", {"track_ids": [12, 13]})]


def test_device_refusal_is_reported(dev):
    dev["ack"] = {"ack_status": "no_music_queue", "ack_detail": "current player: Book"}
    assert "refused it: no_music_queue" in run(dj_toolkit.dj_remove(indexes=[2]))


def test_old_build_paused_sends_nothing(dev):
    dev["lacks"], dev["state"]["playing"] = True, False
    assert "NOTHING was sent" in run(dj_toolkit.dj_remove(indexes=[2]))
    assert dev["sent"] == []


def test_old_build_playing_schedules_boundary_swap(dev, monkeypatch):
    dev["lacks"] = True
    seen = {}

    async def fake_swap(ids, start_id, wait):
        seen.update(ids=ids, start=start_id)

    monkeypatch.setattr(mcp, "_boundary_swap", fake_swap)

    async def go():
        out = await dj_toolkit.dj_remove(indexes=[2])
        await asyncio.sleep(0)
        await mcp._SWAP["task"]
        return out

    assert "when the current song ends" in run(go())
    assert seen == {"ids": [12, 13], "start": 10} and dev["sent"] == []


def test_ban_also_removes_from_queue(dev):
    out = run(dj_toolkit.dj_ban([11], reason="cheesy"))
    assert dev["posts"][0] == ("/api/playback/bans", {"track_ids": [11], "reason": "cheesy", "persona": None})
    assert dev["sent"] == [("replace_upcoming", {"track_ids": [12, 13]})]
    assert "Banned 1" in out and "dj_unban" in out


def test_bans_and_unban(dev, monkeypatch):
    assert "id 11  t11 - a  (no)" in run(dj_toolkit.dj_bans())
    calls = []

    async def fake_del(path, body):
        calls.append((path, body))
        return {"unbanned": [11], "not_banned": []}

    monkeypatch.setattr(mcp, "_delete_json", fake_del)
    assert run(dj_toolkit.dj_unban([11])) == "Unbanned 1."
    assert calls == [("/api/playback/bans", {"track_ids": [11]})]


def test_pool_lane_tool_reports_lanes(dev, monkeypatch):
    async def fake_patch(path, body):
        return {"lane": "Jazz", "action": "pause",
                "lanes": [{"label": "Rock"}, {"label": "Jazz", "paused": True}]}

    monkeypatch.setattr(mcp, "_patch", fake_patch)
    assert run(dj_toolkit.dj_pool_lane("jazz")) == "Lane 'Jazz' paused. Lanes now: Rock, Jazz (paused)."


def test_toolkit_never_touches_the_pool_in_process():
    src = (ROOT / "audiplex_mcp" / "dj_toolkit.py").read_text(encoding="utf-8")
    assert "get_pool" not in src and "audiplex.dj_pool" not in src


# ----- MCP: rider (e): search / tracks source kinds ----------------------


def test_search_and_tracks_source_kinds(monkeypatch):
    async def fake_all():
        return [{"id": 1, "title": "Daisy", "artist_name": "Ashnikko", "duration_seconds": 200},
                {"id": 2, "title": "Other", "artist_name": "Ashnikko", "duration_seconds": 200}]

    monkeypatch.setattr(mcp, "_all_music_tracks", fake_all)
    label, tracks = run(mcp._resolve_source("search", "ashnikko daisy"))
    assert [t["id"] for t in tracks] == [1] and "search" in label
    _, tracks = run(mcp._resolve_source("tracks", "12, 34 56"))
    assert [t["id"] for t in tracks] == [12, 34, 56]


def test_unknown_kind_lists_valid_kinds_in_pool_refusal():
    lanes, empty = run(mcp._resolve_lanes([{"kind": "vibes", "query": "x", "label": "V"}]))
    assert lanes == {"V": []}
    assert "'search', 'tag', 'rated', or 'tracks'" in empty[0]  # #2806  # #3576


# ----- MCP: S3 crossfade (PC renderer only) -------------------------------  # #2806


def test_crossfade_sends_clamped_seconds_and_reports(dev):  # #2806
    dev["ack"] = {"ack_status": "ok", "ack_detail": "crossfade 12s"}
    out = run(dj_toolkit.dj_crossfade(30))
    assert dev["sent"][-1] == ("set_crossfade", {"seconds": 12.0})
    assert "crossfade 12s" in out and "next song change" in out
    dev["ack"] = {"ack_status": "ok", "ack_detail": "crossfade 0s"}
    assert "Crossfade off" in run(dj_toolkit.dj_crossfade(0))


def test_crossfade_on_the_phone_says_it_cant(dev):  # #2806
    dev["ack"] = {"ack_status": "unknown_type", "ack_detail": "set_crossfade"}
    out = run(dj_toolkit.dj_crossfade(6))
    assert "can't crossfade" in out and "PC" in out
