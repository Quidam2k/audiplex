"""#2806 S2: DJ tags (applied, never inferred) and measured-energy sets.

The arc orderer is pure; the server filters banned/non-music/untagged/unmeasured
and says how many it left out; dj_energy_set hands the ordered list to dj_mix
unshuffled, replacing what's after the current song.
"""

import asyncio
import sys
from pathlib import Path

import pytest

from audiplex.energy_arc import order_arc
from audiplex.models import Track

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from audiplex_mcp import dj_toolkit, server as mcp  # noqa: E402


def run(coro):
    return asyncio.run(coro)


def _track(db, album, title, energy=None, seconds=200):
    t = Track(title=title, album_id=album.id, artist_id=album.artist_id, energy=energy,
              file_path=f"/m/{title}.mp3", file_size=1, duration_seconds=seconds)
    db.add(t)
    db.commit()
    return t


# ----- pure arc ordering -------------------------------------------------

T = [(i, e, 240) for i, e in enumerate([50, 10, 90, 30, 70, 20, 80, 60, 40])]
E = {i: e for i, e, _ in T}


def test_rise_and_wind_down_are_monotonic():
    rise = [E[i] for i in order_arc(T, "rise", seed=1)]
    assert rise == sorted(rise) and len(rise) == 9
    down = [E[i] for i in order_arc(T, "wind_down", seed=1)]
    assert down == sorted(down, reverse=True)


def test_peak_tops_out_two_thirds_in_and_comes_down():
    peak = [E[i] for i in order_arc(T, "peak", seed=1)]
    top = peak.index(90)
    assert 4 <= top <= 7 and peak[:top + 1] == sorted(peak[:top + 1])
    assert peak[top:] == sorted(peak[top:], reverse=True)


def test_minutes_samples_then_orders_and_steady_hugs_the_median():
    rise = order_arc(T, "rise", minutes=12, seed=3)  # 12 min of 4-min songs
    assert len(rise) == 3 and [E[i] for i in rise] == sorted(E[i] for i in rise)
    steady = order_arc(T, "steady", minutes=12, seed=3)
    assert sorted(E[i] for i in steady) == [40, 50, 60]


def test_unknown_arc_raises():
    with pytest.raises(ValueError, match="rise"):
        order_arc(T, "bananas")


# ----- server: tags and arcs --------------------------------------------


def test_tags_roundtrip_and_normalized(client, db_session, sample_album):
    a, b = _track(db_session, sample_album, "A", 20), _track(db_session, sample_album, "B")
    r = client.post("/api/playback/tags", json={"track_ids": [a.id, b.id, 9999],
                                                "tags": [" Chill ", "chill", "Rainy  Day"]}).json()
    assert r["tags"] == ["chill", "rainy day"] and r["added"] == 4 and r["unknown"] == [9999]
    assert client.post("/api/playback/tags", json={"track_ids": [a.id], "tags": ["chill"]}).json()["added"] == 0
    assert client.get("/api/playback/tags").json() == [
        {"tag": "chill", "count": 2}, {"tag": "rainy day", "count": 2}]
    rows = client.get("/api/playback/tags/CHILL").json()
    assert [(x["track_id"], x["energy"]) for x in rows] == [(a.id, 20), (b.id, None)]
    r = client.request("DELETE", "/api/playback/tags", json={"track_ids": [a.id], "tags": ["chill"]})
    assert r.json() == {"removed": 1}
    assert client.request("DELETE", "/api/playback/tags", json={"track_ids": [b.id]}).json() == {"removed": 2}
    assert client.post("/api/playback/tags", json={"track_ids": [a.id], "tags": ["  "]}).status_code == 400


def test_arc_filters_and_counts_what_it_left_out(client, db_session, sample_album):
    lo, mid, hi = (_track(db_session, sample_album, n, e) for n, e in (("lo", 10), ("mid", 50), ("hi", 90)))
    raw = _track(db_session, sample_album, "raw")  # never measured
    banned = _track(db_session, sample_album, "banned", 60)
    plain = _track(db_session, sample_album, "plain", 40)
    client.post("/api/playback/bans", json={"track_ids": [banned.id]})
    ids = [lo.id, mid.id, hi.id, raw.id, banned.id, plain.id]
    client.post("/api/playback/tags", json={"track_ids": [lo.id, mid.id, hi.id, raw.id], "tags": ["ride"]})
    r = client.post("/api/playback/energy/arc",
                    json={"track_ids": ids, "arc": "wind_down", "tags": ["ride"]}).json()
    assert r["ordered"] == [hi.id, mid.id, lo.id] and r["energies"] == [90, 50, 10]
    assert (r["unmeasured"], r["untagged"], r["dropped"]) == (1, 1, 1)
    r = client.post("/api/playback/energy/arc", json={"track_ids": ids, "arc": "rise",
                                                      "min_energy": 30, "max_energy": 60}).json()
    assert r["ordered"] == [plain.id, mid.id] and r["out_of_range"] == 2
    assert client.post("/api/playback/energy/arc", json={"track_ids": ids, "arc": "x"}).status_code == 400


def test_energy_column_migrates_on_an_old_db(tmp_path):
    import sqlite3

    from sqlalchemy import create_engine, inspect

    from audiplex.database import _migrate_track_energy

    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE tracks (id INTEGER PRIMARY KEY, title TEXT)")
    con.commit()
    con.close()
    eng = create_engine(f"sqlite:///{path}")
    _migrate_track_energy(eng)
    _migrate_track_energy(eng)  # idempotent
    assert "energy" in {c["name"] for c in inspect(eng).get_columns("tracks")}
    eng.dispose()


# ----- MCP ----------------------------------------------------------------


@pytest.fixture
def api(monkeypatch):
    d = {"posts": [], "mix": None}

    async def fake_get(path):
        if path == "/api/playback/tags":
            return [{"tag": "chill", "count": 2}]
        if path.startswith("/api/playback/tags/"):
            return [{"track_id": 7, "title": "Seven", "artist": "X", "energy": None}]
        return []

    async def fake_post(path, body):
        d["posts"].append((path, body))
        if path == "/api/playback/energy/arc":
            return {"ordered": [3, 1, 2], "energies": [20, 60, 40], "minutes": 12.0,
                    "unmeasured": 2, "untagged": 0, "out_of_range": 0, "dropped": 1}
        return {"tracks": body["track_ids"], "tags": body["tags"], "added": 1, "unknown": []}

    async def fake_resolve(kind, query, recursive=True):
        if kind == "tag" and query == "none":
            raise LookupError("No tracks tagged 'none'.")
        return f"{kind} '{query}'", [{"id": 1}, {"id": 2}, {"id": 3}]

    async def fake_recent(ids, hours):
        return [i for i in ids if i != 2], 1

    async def fake_mix(**kw):
        d["mix"] = kw
        return "MIXED"

    monkeypatch.setattr(mcp, "_get", fake_get)
    monkeypatch.setattr(mcp, "_post", fake_post)
    monkeypatch.setattr(mcp, "_resolve_source", fake_resolve)
    monkeypatch.setattr(mcp, "_recent_split", fake_recent)
    monkeypatch.setattr(mcp, "dj_mix", fake_mix)
    return d


def test_energy_set_orders_then_replaces_the_tail_unshuffled(api):
    out = run(dj_toolkit.dj_energy_set("peak", sources=[{"kind": "folder", "query": "/m"}],
                                       minutes=12, tags=["Chill"]))
    path, body = api["posts"][-1]
    assert path == "/api/playback/energy/arc" and body["track_ids"] == [1, 3] and body["arc"] == "peak"
    assert body["tags"] == ["Chill"] and body["minutes"] == 12
    assert api["mix"] == {"track_ids": [3, 1, 2], "shuffle": False, "keep_upcoming": False,
                          "exclude_recent_hours": 0}
    assert "20 -> 60 -> 40" in out and "2 not measured yet" in out and out.endswith("MIXED")


def test_energy_set_refuses_an_empty_source_and_sends_nothing(api):
    out = run(dj_toolkit.dj_energy_set("rise", sources=[{"kind": "tag", "query": "none"}]))
    assert out.startswith("REFUSED") and "No tracks tagged" in out
    assert api["posts"] == [] and api["mix"] is None


def test_tag_tools(api):
    assert "Tagged 1 track(s) chill" in run(dj_toolkit.dj_tag([5], ["chill"]))
    assert run(dj_toolkit.dj_tags()) == "Tags: chill (2)"
    assert "energy ?" in run(dj_toolkit.dj_tags("chill"))


def test_tag_is_a_mix_and_pool_source_kind(monkeypatch):
    async def fake_get(path):
        assert path == "/api/playback/tags/rainy%20day"
        return [{"track_id": 4}, {"track_id": 8}]

    monkeypatch.setattr(mcp, "_get", fake_get)
    label, tracks = run(mcp._resolve_source("tag", "rainy day"))
    assert label == "tag 'rainy day'" and [t["id"] for t in tracks] == [4, 8]


def test_dj_mix_keep_upcoming_false_replaces_the_tail(monkeypatch):
    sent = {}

    async def fake_get(path):
        return {"playing": True, "track": {"id": 10}, "queue_index": 0,
                "queue": [{"index": 0, "id": 10}, {"index": 1, "id": 11}, {"index": 2, "id": 12}]}

    async def fake_post(path, body):
        sent["plan"] = body
        return {"upcoming": body["new_ids"], "summary": "ok"}

    async def fake_delete(path):
        return {}

    async def fake_recent(ids, hours):
        return ids, 0

    async def fake_enqueue(t, p):
        return "HELD"

    async def lacks():
        return False

    monkeypatch.setattr(mcp, "_get", fake_get)
    monkeypatch.setattr(mcp, "_post", fake_post)
    monkeypatch.setattr(mcp, "_delete", fake_delete)
    monkeypatch.setattr(mcp, "_recent_split", fake_recent)
    monkeypatch.setattr(mcp, "_enqueue", fake_enqueue)
    monkeypatch.setattr(mcp, "_device_lacks_replace_upcoming", lacks)
    monkeypatch.setattr(mcp, "_held_result", lambda d: d)
    monkeypatch.setattr(mcp, "_save_source_map", lambda *a: None)
    run(mcp.dj_mix(track_ids=[20, 21], shuffle=False, keep_upcoming=False, exclude_recent_hours=0))
    assert sent["plan"]["upcoming_ids"] == [] and sent["plan"]["current_id"] == 10
    run(mcp.dj_mix(track_ids=[20, 21], shuffle=False, exclude_recent_hours=0))
    assert sent["plan"]["upcoming_ids"] == [11, 12]
