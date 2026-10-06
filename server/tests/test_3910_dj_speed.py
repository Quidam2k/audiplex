"""#3910 / #7108: catalog paths the DJ hits stay fast on a big library; pool lanes shuffle, recycle and say why they ran dry."""

import asyncio
import random
import sys
import time
from collections import Counter
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest

from audiplex import dj_pool
from audiplex.models import Album, Artist, Track
from audiplex.routers.music import get_music_roots

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from audiplex_mcp import server as mcp_server


def _big_catalog(db):
    artist = Artist(name="Catalog Artist")
    db.add_all([artist])
    db.flush()
    albums = []
    tracks = []
    for g in range(10):
        for a in range(20):
            folder_artist = f"Ren Faire Artist{a}" if (g, a) == (3, 7) else f"Artist{a}"
            for n in range(10):
                album_id = len(albums) + 1
                folder = f"H:\\Music\\Genre{g}\\{folder_artist}\\Album{n}"
                albums.append(
                    Album(
                        id=album_id,
                        title=f"Album {album_id}",
                        artist_id=artist.id,
                        folder_path=folder,
                    )
                )
                for k in range(1, 11):
                    i = len(tracks)
                    tracks.append(
                        Track(
                            title=f"Song {i}",
                            album_id=album_id,
                            artist_id=artist.id,
                            disc_number=1,
                            track_number=k,
                            duration_seconds=200.0,
                            file_path=f"{folder}\\Song{i}.mp3",
                            file_size=1,
                        )
                    )
    db.bulk_save_objects(albums)
    db.bulk_save_objects(tracks)
    db.commit()


@pytest.fixture
def big_catalog(client, db_session):
    client.app.dependency_overrides[get_music_roots] = lambda: ["H:/Music"]
    try:
        _big_catalog(db_session)
        yield client
    finally:
        client.app.dependency_overrides.pop(get_music_roots, None)


def _pool(tmp_path, lanes: dict[str, list[int]], ahead, **kw):
    pool = dj_pool.DJPool(state_file=tmp_path / "pool.json")
    flat_ids = [track_id for ids in lanes.values() for track_id in ids]
    pool.set_pool(
        1,
        flat_ids,
        {},
        lanes=dj_pool.lanes_from_ids(lanes),
        ahead=ahead,
        exclude_recent_hours=0,
        **kw,
    )
    return pool


def test_folder_tracks_loads_only_that_folder_fast(big_catalog):
    """A genre subtree returns its 2,000 tracks within five seconds."""
    path = quote("H:/Music/Genre1", safe="")
    started = time.perf_counter()
    response = big_catalog.get(f"/api/music/folders/tracks?path={path}")
    elapsed = time.perf_counter() - started
    assert response.status_code == 200, response.text
    tracks = response.json()
    expected = 20 * 10 * 10
    assert len(tracks) == expected
    assert len({track["id"] for track in tracks}) == expected
    assert elapsed < 5.0


def test_folder_match_endpoint_one_pass(big_catalog):
    """Folder matching returns only topmost matches within five seconds."""
    for query, expected in (
        ("FAIRE", ["H:/Music/Genre3/Ren Faire Artist7"]),
        ("genre1", ["H:/Music/Genre1"]),
        ("nothing-like-this", []),
    ):
        started = time.perf_counter()
        response = big_catalog.get(f"/api/music/folders/match?q={quote(query, safe='')}")
        elapsed = time.perf_counter() - started
        assert response.status_code == 200, response.text
        assert response.json() == expected
        assert elapsed < 5.0


def test_mcp_folder_match_and_search_under_5s(big_catalog, monkeypatch):
    """MCP resolves folders and searches quickly, then reuses its track cache."""
    calls = []

    async def fake_get(path):
        calls.append(path)
        response = big_catalog.get(path)
        response.raise_for_status()
        return response.json()

    monkeypatch.setattr(mcp_server, "_get", fake_get)
    monkeypatch.setattr(mcp_server, "_all_tracks_cache", None)

    started = time.perf_counter()
    label, tracks = asyncio.run(mcp_server._resolve_source("folder_match", "faire"))
    elapsed = time.perf_counter() - started
    assert "(1 folder(s))" in label
    assert len(tracks) == 100
    assert elapsed < 5.0

    calls.clear()
    started = time.perf_counter()
    output = asyncio.run(mcp_server.dj_search("song 1999"))
    elapsed = time.perf_counter() - started
    assert "match(es)" in output
    assert calls
    assert elapsed < 5.0

    calls.clear()
    output = asyncio.run(mcp_server.dj_search("song 1999"))
    assert "match(es)" in output
    assert calls == []


def test_mcp_folder_match_falls_back_on_404(client, db_session, monkeypatch):
    """Older servers without the folder-match endpoint still resolve via the walk."""
    client.app.dependency_overrides[get_music_roots] = lambda: ["H:/Music"]
    artist = Artist(name="Walker")
    db_session.add(artist)
    db_session.flush()
    for folder in (r"H:\Music\Ren Faire\A", r"H:\Music\Rock\B"):
        album = Album(title=folder, artist_id=artist.id, folder_path=folder)
        db_session.add(album)
        db_session.flush()
        db_session.add_all([Track(title=f"t{k}", album_id=album.id, artist_id=artist.id,
                                  disc_number=1, track_number=k, duration_seconds=200.0,
                                  file_path=f"{folder}/{k}.mp3", file_size=1) for k in range(50)])
    db_session.commit()
    big_catalog = client
    missing_endpoint_calls = []

    async def fake_get(path):
        if path.startswith("/api/music/folders/match"):
            missing_endpoint_calls.append(path)
            raise httpx.HTTPStatusError(
                "404", request=None, response=httpx.Response(404)
            )
        response = big_catalog.get(path)
        response.raise_for_status()
        return response.json()

    monkeypatch.setattr(mcp_server, "_get", fake_get)
    try:
        label, tracks = asyncio.run(mcp_server._resolve_source("folder_match", "faire"))
    finally:
        client.app.dependency_overrides.pop(get_music_roots, None)
    assert missing_endpoint_calls
    assert "(1 folder(s))" in label
    assert len({track["id"] for track in tracks}) == 50


def test_resolve_lanes_budget_skips_slow_source(monkeypatch):
    """A timed-out source leaves the fast lane intact and reports its label."""
    async def fake_resolve(kind, query, recursive=True):
        if query == "slow":
            await asyncio.sleep(5)
            return "x", [{"id": 1}]
        return query, [{"id": 2}]

    monkeypatch.setattr(mcp_server, "_resolve_source", fake_resolve)
    slow = []
    started = time.perf_counter()
    lanes, empty = asyncio.run(
        mcp_server._resolve_lanes(
            [
                {"kind": "folder", "query": "fast"},
                {"kind": "folder", "query": "slow"},
            ],
            budget_s=0.5,
            timed_out=slow,
        )
    )
    elapsed = time.perf_counter() - started
    assert lanes == {"fast": [2]}
    assert empty == []
    assert slow == ["slow"]
    assert elapsed < 2.0


def test_lane_picks_are_shuffled(tmp_path):
    """A full lane produces a shuffled permutation of its tracks."""
    random.seed(1)
    pool = _pool(tmp_path, {"a": list(range(1, 51))}, ahead=50)
    picks = pool.top_up(
        current_track_id=999, upcoming_track_ids=[999], db=None
    )["picks"]
    assert sorted(picks) == list(range(1, 51))
    assert picks != sorted(picks)


def test_small_lanes_recycle_and_keep_their_turn(tmp_path):
    """Small lanes recycle evenly while respecting the ten-pick repeat window."""
    small_ids = list(range(1, 12))
    pool = _pool(
        tmp_path,
        {"big": list(range(1000, 1300)), "small": small_ids},
        ahead=120,
        no_repeat_picks=10,
    )
    picks = pool.top_up(
        current_track_id=999, upcoming_track_ids=[999], db=None
    )["picks"]
    assert len(picks) == 120
    counts = Counter(track_id for track_id in picks if track_id in small_ids)
    assert sum(counts.values()) >= 50
    assert all(counts[track_id] >= 2 for track_id in small_ids)
    last_positions = {}
    for position, track_id in enumerate(picks):
        if track_id in last_positions:
            assert position - last_positions[track_id] - 1 >= 10
        last_positions[track_id] = position


def test_no_repeat_window_blocks_tiny_lane_and_explains(tmp_path):
    """A tiny lane exhausts once its tracks fall inside the repeat window."""
    pool = _pool(
        tmp_path,
        {"big": list(range(1000, 1300)), "tiny": [1, 2]},
        ahead=20,
        no_repeat_picks=30,
    )
    result = pool.top_up(
        current_track_id=999, upcoming_track_ids=[999], db=None
    )
    assert len(result["picks"]) == 20
    assert Counter(track_id for track_id in result["picks"] if track_id in {1, 2}) == {
        1: 1,
        2: 1,
    }
    tiny = next(
        lane for lane in result["per_lane_details"] if lane["label"] == "tiny"
    )
    assert "within the last 30 picks" in tiny["why_empty"]


def test_single_lane_pool_does_not_loop(tmp_path):
    """A lone lane stops after its three fresh tracks instead of recycling."""
    pool = _pool(tmp_path, {"a": [1, 2, 3]}, ahead=10)
    picks = pool.top_up(
        current_track_id=999, upcoming_track_ids=[999], db=None
    )["picks"]
    assert len(picks) == 3
    assert sorted(picks) == [1, 2, 3]


def test_zero_lane_says_why():
    """Empty lanes distinguish filtered tracks from sources resolving no tracks."""
    arcane = dj_pool.lanes_from_ids(
        {"arcane": []}, {"arcane": {"non-music": 11}}
    )["arcane"]
    assert "11" in arcane["why_empty"]
    assert "non-music" in arcane["why_empty"]
    assert dj_pool.lanes_from_ids({"x": []})["x"]["why_empty"] == (
        "the source resolved 0 tracks"
    )


def test_set_pool_route_reports_dropped_reason(
    client, db_session, tmp_path, monkeypatch
):
    """The pool route explains a lane emptied by filtering overly long tracks."""
    pool = dj_pool.DJPool(state_file=tmp_path / "pool.json")
    monkeypatch.setattr(dj_pool, "get_pool", lambda: pool)
    artist = Artist(name="Long Tracks Artist")
    db_session.add(artist)
    db_session.flush()
    album = Album(
        title="Long Tracks",
        artist_id=artist.id,
        folder_path="H:\\Music\\Long Tracks",
    )
    db_session.add(album)
    db_session.flush()
    tracks = [
        Track(
            title=f"Long Song {k}",
            album_id=album.id,
            artist_id=artist.id,
            disc_number=1,
            track_number=k,
            duration_seconds=4 * 3600,
            file_path=f"H:\\Music\\Long Tracks\\Song{k}.mp3",
            file_size=1,
        )
        for k in (1, 2)
    ]
    db_session.add_all(tracks)
    db_session.flush()
    ids = [track.id for track in tracks]
    db_session.commit()

    response = client.post(
        "/api/playback/pool",
        json={"lanes": {"long": ids}, "ahead": 5, "prime_current_id": 0},
    )
    assert response.status_code == 200, response.text
    long_lane = next(
        lane
        for lane in response.json()["per_lane_details"]
        if lane["label"] == "long"
    )
    assert "too long" in long_lane["why_empty"]



def test_window_blocked_lane_returns_within_one_long_top_up(tmp_path):
    """A one-track lane comes back once the window passes, inside the same refill."""
    pool = _pool(tmp_path, {"big": list(range(1000, 1300)), "one": [1]}, ahead=100, no_repeat_picks=30)
    picks = pool.top_up(current_track_id=999, upcoming_track_ids=[999], db=None)["picks"]
    positions = [i for i, t in enumerate(picks) if t == 1]
    assert len(positions) >= 3
    assert all(b - a > 30 for a, b in zip(positions, positions[1:]))
