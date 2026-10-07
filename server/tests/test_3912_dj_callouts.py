"""#3912: the DJ names deep cuts from whole albums and stays quiet on songs Todd knows."""

import pytest

from audiplex import dj_callouts
from audiplex.models import Album, Artist, Track, User
from audiplex.routers import playback

ALBUM = r"H:\Stacked Deck\Music\Artists & Albums\Rock\The Police\Synchronicity\05 - Mother.mp3"
FASTER = r"Q:\Stacked Deck Music\Individual\faster\x.mp3"


@pytest.fixture(autouse=True)
def state_file(tmp_path, monkeypatch):
    path = tmp_path / "callouts.json"
    monkeypatch.setenv("AUDIPLEX_DJ_CALLOUTS_STATE", str(path))
    return path


@pytest.mark.parametrize("path,expected", [
    (ALBUM, True),
    (ALBUM.replace("\\", "/"), True),
    (r"H:\Stacked Deck\Music\Artists & Albums\Rock\Billboard Countdowns\Billboard 1978 Top 100 Hits\x.mp3", False),
    (r"H:\Stacked Deck\Music\Artists & Albums\Rock\Top 600 Modern Rock Songs\x.mp3", False),
    (r"Q:\Stacked Deck Music\Artists & Albums\Mixed\Ren Faire\x.mp3", False),
    (FASTER, False),
    (r"Q:\Stacked Deck Music\Todd Walker - music\x.mp3", False),
    (None, False),
])
def test_default_for_path(path, expected):
    assert dj_callouts.default_for_path(path)[0] is expected


def test_lane_override_beats_path_both_ways():
    state = dj_callouts.load_state()
    dj_callouts.set_source(state, "bucket 'arcane'", "silent")
    dj_callouts.set_source(state, "folder 'faster'", "callout")
    assert dj_callouts.decide({"id": 1, "path": ALBUM}, "bucket 'arcane'", state)["callout"] is False
    assert dj_callouts.decide({"id": 2, "path": FASTER}, "folder 'faster'", state)["callout"] is True
    assert dj_callouts.set_source(state, "folder 'faster'", "auto") is None
    assert dj_callouts.decide({"id": 2, "path": FASTER}, "folder 'faster'", state)["callout"] is False
    with pytest.raises(ValueError):
        dj_callouts.set_source(state, "x", "loud")


def test_exceptions_beat_a_silent_lane_and_count_down():
    state = dj_callouts.load_state()
    dj_callouts.set_source(state, "folder 'faster'", "silent")
    dj_callouts.add_exception(state, "track", 7, "Mother - The Police")
    dj_callouts.add_exception(state, "track", 7, "Mother - The Police")  # replaces, no dup
    assert len(state["exceptions"]) == 1
    track = {"id": 7, "path": FASTER, "artist": "x"}
    for _ in range(3):
        assert dj_callouts.decide(track, "folder 'faster'", state)["exception"] is True
        assert dj_callouts.consume(state, track)
    assert state["exceptions"] == []
    assert dj_callouts.decide(track, "folder 'faster'", state)["callout"] is False


def test_artist_exception_ignores_the_and_case():
    state = dj_callouts.load_state()
    dj_callouts.add_exception(state, "artist", "The Police", "The Police")
    assert dj_callouts.decide({"id": 1, "path": FASTER, "artist": "police"}, None, state)["callout"] is True
    with pytest.raises(ValueError):
        dj_callouts.add_exception(state, "artist", "", "")


def test_state_round_trip_and_corrupt_file(state_file):
    state = dj_callouts.load_state()
    dj_callouts.set_source(state, "lane", "callout")
    dj_callouts.save_state(state)
    assert dj_callouts.load_state()["sources"] == {"lane": "callout"}
    state_file.write_text("{nope", encoding="utf-8")
    assert dj_callouts.load_state() == {"sources": {}, "exceptions": []}


def _seed(db_session):
    db_session.add(User(username="todd", password_hash="x", display_name="T", is_admin=True))
    art = Artist(name="The Police")
    db_session.add(art)
    db_session.flush()
    alb = Album(title="Synchronicity", artist_id=art.id, folder_path="/x")
    db_session.add(alb)
    db_session.flush()
    ids = []
    for title, path in (("Mother", ALBUM), ("Roxanne", FASTER)):
        t = Track(title=title, album_id=alb.id, artist_id=art.id, disc_number=1, track_number=1,
                  duration_seconds=200.0, file_path=path)
        db_session.add(t)
        db_session.flush()
        ids.append(t.id)
    db_session.commit()
    return ids


def test_routes(client, db_session, monkeypatch):
    deep, known = _seed(db_session)
    monkeypatch.setattr(playback, "_lane_of", lambda tid: "folder 'faster'" if tid == known else None)
    r = client.get(f"/api/playback/callouts?ids={deep},{known}").json()["tracks"]
    assert r[str(deep)]["callout"] is True and r[str(known)]["callout"] is False
    assert r[str(known)]["lane"] == "folder 'faster'" and r[str(deep)]["artist"] == "The Police"

    ex = client.post("/api/playback/callouts/exception", json={"track_id": known, "scope": "track"}).json()
    assert ex["exception"]["remaining"] == 3 and ex["track"]["title"] == "Roxanne"
    for left in (2, 1, 0):
        r = client.get(f"/api/playback/callouts?ids={known}&consume=true").json()
        assert r["tracks"][str(known)]["callout"] is True
        assert sum(e["remaining"] for e in r["exceptions"]) == left
    assert client.get(f"/api/playback/callouts?ids={known}").json()["tracks"][str(known)]["callout"] is False
    assert client.post("/api/playback/callouts/exception", json={"track_id": 999}).status_code == 404

    r = client.put("/api/playback/callouts/source", json={"source": "folder 'faster'", "mode": "callout"})
    assert r.json()["mode"] == "callout"
    assert client.get(f"/api/playback/callouts?ids={known}").json()["tracks"][str(known)]["callout"] is True
    assert client.put("/api/playback/callouts/source", json={"source": "x", "mode": "bad"}).status_code == 400
