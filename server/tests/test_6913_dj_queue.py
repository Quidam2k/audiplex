"""Regression coverage for #6913 DJ queue refills, replanning, and stop latches."""

import time

import pytest

from audiplex import dj_pool, dj_triggers, playback_bus, scheduled_stop
from audiplex.models import Album, Artist, Track
from audiplex.playback_bus import bus
from audiplex.scheduled_stop import StopController


def _track(db, title, *, file_hash=None, artist="Band"):
    art = db.query(Artist).filter(Artist.name == artist).first()
    if art is None:
        art = Artist(name=artist)
        db.add(art)
        db.flush()
    alb = db.query(Album).filter(Album.artist_id == art.id).first()
    if alb is None:
        alb = Album(
            title="LP",
            artist_id=art.id,
            genre="Rock",
            folder_path=f"/fake/{artist}",
        )
        db.add(alb)
        db.flush()
    track = Track(
        title=title,
        album_id=alb.id,
        artist_id=art.id,
        disc_number=1,
        track_number=db.query(Track).count() + 1,
        duration_seconds=200.0,
        file_path=f"/fake/{artist}/{title}.mp3",
        file_hash=file_hash,
    )
    db.add(track)
    db.commit()
    return track.id


def _state(current, after=(), playing=True):
    queue = [{"index": 0, "id": current}] + [
        {"index": i + 1, "id": track_id}
        for i, track_id in enumerate(after)
    ]
    return {
        "playing": playing,
        "track": {"id": current, "title": "x"},
        "queue_index": 0,
        "queue": queue,
    }


def _queued():
    return [
        (row["type"], row["payload"]["track_ids"])
        for row in _rows()
        if row["type"] == "queue"
    ]


def _rows():
    out = []
    for record in bus._commands.values():
        out.append({"type": record.type, "payload": record.payload})
    return out


def _types():
    return [command["type"] for command in bus.commands(200)]


def _last(command_type):
    return [
        command
        for command in bus.commands(200)
        if command["type"] == command_type
    ][-1]


@pytest.fixture(autouse=True)
def fresh():
    bus.reset()
    scheduled_stop.controller.reset()
    yield
    bus.reset()
    scheduled_stop.controller.reset()


@pytest.fixture
def pool_db(db_session, monkeypatch):
    """Route the bus hook's DB session to the test DB (never the real one)."""

    class _NoClose:
        def __init__(self, session):
            self._session = session

        def __getattr__(self, key):
            return getattr(self._session, key)

        def close(self):
            pass

    monkeypatch.setattr(
        playback_bus,
        "_pool_session",
        lambda: _NoClose(db_session),
    )
    bus.reset()
    yield db_session
    bus.reset()


def _start(lanes, ahead=3, **kwargs):
    pool = dj_pool.get_pool()
    pool.set_pool(
        7,
        [track_id for values in lanes.values() for track_id in values],
        {},
        lanes=dj_pool.lanes_from_ids(lanes),
        ahead=ahead,
        **kwargs,
    )
    return pool


def test_refill_at_waits_then_tops_up_in_ordered_chunks(pool_db, monkeypatch):
    ids = [_track(pool_db, f"s{i}") for i in range(20)]
    pool = _start({"a": ids}, ahead=10, refill_at=3)
    monkeypatch.setattr(playback_bus, "QUEUE_CHUNK", 3)

    bus.set_state(_state(ids[0], after=ids[1:6]))
    assert _queued() == []

    bus.set_state(_state(ids[6], after=ids[7:9]))
    chunks = [track_ids for _, track_ids in _queued()]
    picks = [track_id for chunk in chunks for track_id in chunk]
    assert [len(chunk) for chunk in chunks] == [3, 3, 2]
    assert picks == pool.state["played_this_session"][-8:]


def test_refill_at_none_keeps_exact_ahead_behavior(pool_db):
    ids = [_track(pool_db, f"s{i}") for i in range(6)]
    _start({"a": ids}, ahead=3, refill_at=None)

    bus.set_state(_state(ids[0], after=[ids[1]]))

    assert _queued() == [("queue", [ids[2], ids[3]])]


def test_send_tracks_chunks_replace_then_queue_with_source(monkeypatch):
    ids = list(range(1, 8))
    monkeypatch.setattr(playback_bus, "QUEUE_CHUNK", 3)

    bus.send_tracks("replace_upcoming", ids, source="test:replan")

    records = list(bus._commands.values())
    assert [
        (record.type, record.payload["track_ids"])
        for record in records
    ] == [
        ("replace_upcoming", [1, 2, 3]),
        ("queue", [4, 5, 6]),
        ("queue", [7]),
    ]
    assert [record.source for record in records] == ["test:replan"] * 3


def test_replan_pool_excludes_paused_lane(pool_db):
    ids = [_track(pool_db, f"s{i}") for i in range(13)]
    lane_a = ids[1:7]
    lane_b = ids[7:13]
    pool = _start({"a": lane_a, "b": lane_b}, ahead=4)
    bus.set_state(_state(ids[0], after=[lane_a[0], lane_b[0], lane_a[1], lane_b[1]]))
    assert bus.commands(200) == []

    pool.set_lane("b", "pause")
    result = bus.replan_pool("t")

    records = list(bus._commands.values())
    assert result["replanned"] is True
    assert [record.type for record in records] == ["replace_upcoming"]
    assert not set(records[0].payload["track_ids"]) & set(lane_b)
    assert records[0].source == "t"


def test_replan_pool_does_nothing_without_current_track(pool_db):
    ids = [_track(pool_db, f"s{i}") for i in range(4)]
    _start({"a": ids}, ahead=3)

    result = bus.replan_pool("t")

    assert result["replanned"] is False
    assert bus.commands(200) == []


def test_replan_pool_does_nothing_while_stop_latched(pool_db):
    ids = [_track(pool_db, f"s{i}") for i in range(6)]
    _start({"a": ids}, ahead=3)
    scheduled_stop.controller.set_latch("stop requested", time.time())
    bus.set_state(_state(ids[0], after=[ids[1]]))

    result = bus.replan_pool("t")

    assert result["replanned"] is False
    assert bus.commands(200) == []


def test_latch_gate_refuses_nonempty_replace_but_allows_empty_replace():
    controller = StopController()
    now = 100.0
    controller.set_latch("stop requested", now)

    refusal = controller.gate(
        "replace_upcoming",
        {"track_ids": [1]},
        now,
    )

    assert refusal is not None
    assert controller.gate(
        "replace_upcoming",
        {"track_ids": []},
        now,
    ) is None


def test_enqueue_records_explicit_and_default_sources():
    bus._enqueue("pause", {}, source="x:y")
    assert bus.commands()[-1]["source"] == "x:y"

    bus._enqueue("pause", {})
    assert bus.commands()[-1]["source"] == "server"


def test_pause_observed_attributes_recent_pause_command(monkeypatch):
    seen = []
    monkeypatch.setattr(
        playback_bus,
        "_append_diag",
        lambda kind, record: seen.append((kind, record)),
    )

    bus.set_state(_state(1, playing=True))
    bus._enqueue("pause", {}, source="p:dj_pause")
    bus.set_state(_state(1, playing=False))

    observed = [record for kind, record in seen if kind == "pause_observed"]
    assert len(observed) == 1
    assert observed[0]["source"] == "p:dj_pause"
    assert observed[0]["type"] == "pause"


def test_pause_observed_attributes_device_without_pause_command(monkeypatch):
    seen = []
    monkeypatch.setattr(
        playback_bus,
        "_append_diag",
        lambda kind, record: seen.append((kind, record)),
    )

    bus.set_state(_state(1, playing=True))
    bus.set_state(_state(1, playing=False))

    observed = [record for kind, record in seen if kind == "pause_observed"]
    assert len(observed) == 1
    assert observed[0]["source"] == "device"
    assert observed[0]["command_id"] is None


def test_queue_track_cue_does_not_refill_while_latched():
    now = time.time()
    scheduled_stop.controller.set_latch("stop requested", now)
    cue = {"actions": [{"type": "queue_track", "track_id": 9}]}

    command_ids = dj_triggers.execute_actions(cue, bus, now)

    assert command_ids == []
    assert "play_next" not in _types()


def test_post_pool_is_refused_while_latched(client):
    scheduled_stop.controller.set_latch("stop requested", time.time())

    response = client.post(
        "/api/playback/pool",
        json={"lanes": {"a": [1, 2]}},
    )

    assert response.status_code == 409


def test_command_route_appends_caller_source(client):
    response = client.post(
        "/api/playback/command",
        json={
            "type": "pause",
            "payload": {},
            "source": "jarvis:dj_pause",
        },
    )

    assert response.status_code == 200
    assert bus.commands()[-1]["source"].endswith("/jarvis:dj_pause")

