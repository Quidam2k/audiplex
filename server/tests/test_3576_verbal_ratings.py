"""#3576: a persona sets Todd's star rating, and the app sees it."""

import pytest
from fastapi import Depends

from audiplex.auth import get_current_user, hash_password
from audiplex.config import get_settings
from audiplex.database import get_db
from audiplex.models import TrackRating, User
from audiplex.routers.playback import stored_stars, verbal_note


def _as(client, username):
    def override(db=Depends(get_db)):
        return db.query(User).filter(User.username == username).first()

    client.app.dependency_overrides[get_current_user] = override


@pytest.fixture
def agent_client(client, db_session, monkeypatch):
    """Requests authenticate as 'dj-agent'; the configured owner is 'testuser'."""
    monkeypatch.setattr(get_settings(), "dj_owner_username", "testuser")
    db_session.add(User(username="dj-agent", password_hash=hash_password("x"),
                        display_name="DJ Agent", is_admin=False))
    db_session.commit()
    _as(client, "dj-agent")
    yield client
    client.app.dependency_overrides[get_current_user] = lambda db=Depends(get_db): db.query(User).first()


def test_stored_stars_are_exact_halves():  # #6117 (was the floor under #3576)
    assert stored_stars(4.5) == 4.5
    assert stored_stars(5) == 5
    assert stored_stars(0.5) == 0.5


def test_note_carries_persona_exact_number_and_words():
    assert verbal_note(4.5, "  four, four and a half ", "Juno") == "[Juno, said 4.5] four, four and a half"
    assert verbal_note(5, "", "") == "[DJ, said 5]"


def test_agent_rates_owner_and_app_sees_it(agent_client, db_session, sample_track):
    resp = agent_client.put("/api/playback/ratings", json={
        "track_ids": [sample_track.id, 99999], "stars": 4.5,
        "words": "four and a half", "persona": "Juno"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["stored"] == 4.5
    assert data["unknown"] == [99999]
    assert data["rated"] == [{"track_id": sample_track.id, "rating": 4.5, "was": None}]

    agent = db_session.query(User).filter(User.username == "dj-agent").first()
    assert db_session.query(TrackRating).filter(TrackRating.user_id == agent.id).count() == 0

    # The app reads per-caller as Todd: the star tap's own endpoint shows it.
    _as(agent_client, "testuser")
    rows = agent_client.get("/api/music/ratings").json()
    # #6117: `rating` stays the whole star for old app builds; `stars` is exact.
    assert [(r["track_id"], r["rating"], r["stars"]) for r in rows] == [(sample_track.id, 4, 4.5)]
    assert rows[0]["note"] == "[Juno, said 4.5] four and a half"


def test_rerate_updates_in_place_and_reports_was(agent_client, sample_track):
    agent_client.put("/api/playback/ratings", json={"track_ids": [sample_track.id], "stars": 3})
    data = agent_client.put("/api/playback/ratings", json={
        "track_ids": [sample_track.id], "stars": 5, "words": "five stars"}).json()
    assert data["rated"] == [{"track_id": sample_track.id, "rating": 5, "was": 3}]
    assert len(agent_client.get("/api/playback/ratings").json()) == 1


@pytest.mark.parametrize("stars", [0, 4.3, 5.5])
def test_rejects_non_half_stars(agent_client, sample_track, stars):
    resp = agent_client.put("/api/playback/ratings", json={"track_ids": [sample_track.id], "stars": stars})
    assert resp.status_code == 422


def test_clear_owner_rating(agent_client, sample_track):
    agent_client.put("/api/playback/ratings", json={"track_ids": [sample_track.id], "stars": 2})
    resp = agent_client.request("DELETE", "/api/playback/ratings", json={"track_ids": [sample_track.id]})
    assert resp.json() == {"deleted": 1}
    assert agent_client.get("/api/playback/ratings").json() == []


# ----- MCP side: dj_star, the 'rated' source, dj_taste --------------------

import asyncio  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from audiplex_mcp import dj_toolkit, server as mcp  # noqa: E402


def test_dj_star_puts_to_owner_endpoint_and_says_what_app_shows(monkeypatch):
    sent = {}

    async def fake_put(path, body):
        sent.update(path=path, body=body)
        return {"stars": 4.5, "stored": 4, "note": "[Juno, said 4.5] wow", "unknown": [],
                "rated": [{"track_id": 3535, "rating": 4, "was": 3}]}

    monkeypatch.setattr(mcp, "_put", fake_put)
    out = asyncio.run(dj_toolkit.dj_star([3535], 4.5, "wow", "Juno"))  # fake replies stored=4
    assert sent == {"path": "/api/playback/ratings",
                    "body": {"track_ids": [3535], "stars": 4.5, "words": "wow", "persona": "Juno"}}
    assert "Rated 1 track(s) 4 stars" in out and "3535 (was 3)" in out


def test_rated_is_a_pool_source_kind(monkeypatch):
    async def fake_get(path):
        assert path == "/api/playback/ratings"
        return [{"track_id": 1, "rating": 5}, {"track_id": 2, "rating": 4}, {"track_id": 3, "rating": 2}]

    monkeypatch.setattr(mcp, "_get", fake_get)
    label, tracks = asyncio.run(mcp._resolve_source("rated", "4"))
    assert label == "rated 4+ stars" and [t["id"] for t in tracks] == [1, 2]
    label, tracks = asyncio.run(mcp._resolve_source("rated", ""))
    assert [t["id"] for t in tracks] == [1, 2]
    with pytest.raises(LookupError):
        asyncio.run(mcp._resolve_source("rated", "5.5"))


def test_dj_taste_shows_star_breakdown_with_names(monkeypatch):
    async def fake_get(path):
        if path == "/api/playback/ratings":
            return [{"track_id": 7, "rating": 5, "note": "[Juno, said 5] all-time great"},
                    {"track_id": 8, "rating": 4, "note": ""}]
        if path.startswith("/api/music/tracks/"):
            return {"artist_name": "Louis Prima", "title": "Sing Sing Sing"}
        return []

    monkeypatch.setattr(mcp, "_get", fake_get)
    out = asyncio.run(mcp.dj_taste())
    assert "Todd's rated tracks (2): 5*: 1, 4*: 1" in out
    assert "7 | Louis Prima - Sing Sing Sing" in out


# ----- backfill script --------------------------------------------------

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import backfill_verbal_ratings as bf  # noqa: E402

MD = """# Pending
| When | Audiplex id(s) | Track | Todd's words | Msg |
|---|---|---|---|---|
| 14:46 | 928, 4679 | Tide | "one of my favorites... 5 stars"; hope | 1 |
| 16:08 | 4932 (tagged 16:40) | Tumbleweeds | "five stars... cowboy" | 2 |
| 16:38 | 3535 | Sondheim | "four, four and a half stars" | 3 |
| 17:00 | 77 | Mystery | "pretty good" | 4 |
"""


@pytest.mark.parametrize("words,stars", [
    ("Five stars.", 5), ("5 stars", 5), ("four, four and a half stars", 4.5),
    ("Pete Seeger, five stars... Little Boxes, five stars", 5), ("pretty good", None)])
def test_stars_from_words(words, stars):
    assert bf.stars_from_words(words) == stars


def test_parse_ignores_times_in_parentheses_and_stamps_once():
    lines, head, rows = bf.parse_table(MD)
    assert [r["ids"] for r in rows] == [[928, 4679], [4932], [3535], [77]]
    md2 = bf.stamp_table(lines, head, {rows[0]["line_no"]: "2026-09-30: 5 (app 5)"})
    lines2, head2, rows2 = bf.parse_table(md2)
    assert [r["applied"] for r in rows2] == [True, False, False, False]
    assert "| Applied |" in lines2[head2]
    # Second stamp reuses the column rather than adding another.
    md3 = bf.stamp_table(lines2, head2, {rows2[2]["line_no"]: "2026-09-30: 4.5 (app 4)"})
    assert md3.count("Applied") == 1
    assert [r["applied"] for r in bf.parse_table(md3)[2]] == [True, False, True, False]


def test_plan_skips_applied_and_unparseable_and_adds_uncovered_tags(db_session, sample_track):
    from audiplex.models import DjTrackTag

    db_session.add_all([DjTrackTag(track_id=sample_track.id, tag="five-star-verbal"),
                        DjTrackTag(track_id=928, tag="five-star-verbal")])
    db_session.commit()
    _, _, rows = bf.parse_table(MD)
    rows[0]["applied"] = True
    todo, warnings = bf.plan(db_session, rows)
    assert [(t["ids"], t["stars"]) for t in todo] == [
        ([4932], 5), ([3535], 4.5), ([sample_track.id], 5)]
    assert len(warnings) == 1 and "pretty good" in warnings[0]


def test_apply_writes_owner_rows(db_session, sample_track):
    owner = db_session.query(User).first()
    report = bf.apply(db_session, owner, [{"ids": [sample_track.id, 424242], "stars": 4.5,
                                           "words": "four and a half", "line_no": None}])
    row = db_session.query(TrackRating).filter_by(user_id=owner.id, track_id=sample_track.id).one()
    assert (row.rating, row.note) == (4.5, "[ride log, said 4.5] four and a half")
    assert any("424242: NOT IN LIBRARY" in r for r in report)


# ----- #6117: halves end to end --------------------------------------------


def test_app_route_takes_halves_and_old_whole_ratings(client, sample_track):
    r = client.put(f"/api/music/tracks/{sample_track.id}/rating", json={"stars": 3.5})
    assert r.status_code == 200 and (r.json()["rating"], r.json()["stars"]) == (3, 3.5)
    # An app build before 1.0.51 still sends a whole `rating`.
    r = client.put(f"/api/music/tracks/{sample_track.id}/rating", json={"rating": 2})
    assert (r.json()["rating"], r.json()["stars"]) == (2, 2.0)
    for bad in ({"stars": 0.3}, {"stars": 5.5}, {}, {"rating": 6}):
        assert client.put(f"/api/music/tracks/{sample_track.id}/rating", json=bad).status_code == 422


def test_old_int_only_client_model_still_parses_list(client, sample_track):
    """An app build before 1.0.51 reads `rating` as Int and ignores unknown
    keys: every row must still carry a whole-number `rating`."""
    client.put(f"/api/music/tracks/{sample_track.id}/rating", json={"stars": 4.5})
    row = client.get("/api/music/ratings").json()[0]
    assert isinstance(row["rating"], int) and row["rating"] == 4


def test_fix_halves_restores_floored_rows(db_session, sample_track):
    owner = db_session.query(User).first()
    db_session.add(TrackRating(user_id=owner.id, track_id=sample_track.id, rating=4,
                               note="[ride log, said 4.5] four, four and a half"))
    db_session.commit()
    assert bf.fix_halves(db_session, owner, apply=False) == [f"  track {sample_track.id}: 4 -> 4.5"]
    assert db_session.query(TrackRating).one().rating == 4
    bf.fix_halves(db_session, owner, apply=True)
    assert db_session.query(TrackRating).one().rating == 4.5
    assert bf.fix_halves(db_session, owner, apply=True) == []  # idempotent


def test_rated_source_uses_exact_stars(monkeypatch):
    async def fake_get(path):
        return [{"track_id": 1, "rating": 4, "stars": 4.5}, {"track_id": 2, "rating": 4, "stars": 4.0}]

    monkeypatch.setattr(mcp, "_get", fake_get)
    _, tracks = asyncio.run(mcp._resolve_source("rated", "4.5"))
    assert [t["id"] for t in tracks] == [1]


def test_pending_in_applied_column_is_not_applied():  # #7230: "pending" rows were skipped forever
    md = MD.replace("| Todd's words | Msg |", "| Todd's words | Msg | Applied |").replace("|---|---|---|---|---|", "|---|---|---|---|---|---|")
    rows = bf.parse_table(md)[2]
    lines = md.splitlines()
    lines[rows[0]["line_no"]] += " 2026-09-30: 5 (app 5) |"
    lines[rows[1]["line_no"]] += " pending |"
    assert [r["applied"] for r in bf.parse_table("\n".join(lines))[2]] == [True, False, False, False]
