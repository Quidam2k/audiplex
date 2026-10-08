"""Pure queue guard tests, plus one in-memory SQLite loader test (#7335)."""

import time
from datetime import datetime, timezone

import pytest

from audiplex.identity import work_key
from audiplex.queue_guard import (
    GuardInput, Play, apply_guard, is_agent_source, load_guard_input,
    work_blocked_keys,
)

NOW = 1_000_000.0

def guard_input(**overrides):
    artists = overrides.pop("artists", {})
    values = dict(
        op="queue", incoming=[], current_id=None, upcoming=[], reserved=[],
        plays=[], ratings={}, titles={}, now=NOW,
    )
    values.update(overrides)
    ids = set(values["incoming"] + values["upcoming"] + values["reserved"])
    ids.update(play.track_id for play in values["plays"])
    if values["current_id"] is not None:
        ids.add(values["current_id"])
    values["titles"] = {tid: values["titles"].get(tid, f"Song {tid}") for tid in ids}
    values.setdefault("work", {
        tid: work_key(values["titles"][tid], artists.get(tid, "Test Artist"))
        for tid in ids
    })
    return GuardInput(**values)

def assert_five_spacing(sequence, ratings):
    positions = [i for i, tid in enumerate(sequence) if ratings.get(tid, 0) >= 5]
    for i, left in enumerate(positions):
        assert all(right - left >= 4 for right in positions[i + 1:])

def assert_cooldown(event, hours, seconds, blocked):
    at = NOW - hours * 3600
    inp = guard_input(
        incoming=[2], plays=[Play(1, at, event, seconds)],
        titles={1: "Driven To Tears", 2: "Driven To Tears (Live)"},
        artists={1: "Ozzy Osbourne", 2: "Ozzy Osbourne"},
    )
    result = apply_guard(inp)
    assert result.kept == ([] if blocked else [2])
    assert result.dropped == ([2] if blocked else [])
    if blocked:
        note = " ".join(result.notes)
        assert "Driven To Tears (Live)" in note
        assert time.strftime("%H:%M", time.localtime(at)) in note
    else:
        assert result.notes == []

def test_studio_and_live_versions_dedupe_by_work():
    studio = work_key("Driven To Tears", "Ozzy Osbourne")
    live = work_key("Driven To Tears (Live)", "Ozzy Osbourne")
    assert studio == live
    result = apply_guard(guard_input(
        incoming=[1, 2], work={1: studio, 2: live},
        titles={1: "Driven To Tears", 2: "Driven To Tears (Live)"},
    ))
    assert result.kept == [1]
    assert result.dropped == [2]
    assert any("Driven To Tears (Live)" in n and "same song" in n for n in result.notes)

def test_covers_by_other_artists_are_kept():
    inp = guard_input(
        incoming=[1, 2], titles={1: "Hurt", 2: "Hurt"},
        artists={1: "Johnny Cash", 2: "Nine Inch Nails"},
    )
    result = apply_guard(inp)
    assert result.kept == [1, 2]
    assert result.dropped == []

def test_complete_two_hours_ago_blocks_same_work():
    assert_cooldown("complete", 2, 0, True)

def test_skip_three_hours_ago_blocks_same_work():
    assert_cooldown("skip", 3, 0, True)

def test_stop_after_45_seconds_blocks_same_work():
    assert_cooldown("stop", 1, 45, True)

def test_stop_after_10_seconds_does_not_block_same_work():
    assert_cooldown("stop", 1, 10, False)

def test_start_one_hour_ago_does_not_block_same_work():
    assert_cooldown("start", 1, 300, False)

def test_complete_25_hours_ago_does_not_block_same_work():
    assert_cooldown("complete", 25, 300, False)

def test_upcoming_work_blocks_incoming_copy():
    result = apply_guard(guard_input(
        upcoming=[1], incoming=[2],
        titles={1: "Driven To Tears", 2: "Driven To Tears (Live)"},
    ))
    assert result.kept == []
    assert result.dropped == [2]
    assert any("is already queued" in n for n in result.notes)

def test_current_work_blocks_incoming_copy():
    result = apply_guard(guard_input(
        current_id=1, incoming=[2],
        titles={1: "Driven To Tears", 2: "Driven To Tears (Live)"},
    ))
    assert result.kept == []
    assert result.dropped == [2]
    assert any("is playing now" in n for n in result.notes)

def test_play_now_does_not_block_current_work():
    result = apply_guard(guard_input(
        op="play_now", current_id=1, incoming=[2, 3],
        titles={1: "Driven To Tears", 2: "Driven To Tears (Live)"},
    ))
    assert result.kept == [2, 3]
    assert result.dropped == []

def test_replace_upcoming_checks_only_surviving_queue():
    # Track 2 was in the old queue; it is absent from the surviving upcoming.
    result = apply_guard(guard_input(
        op="replace_upcoming", current_id=1, upcoming=[], incoming=[2, 1],
    ))
    assert result.kept == [2]
    assert result.dropped == [1]
    assert any("is playing now" in n for n in result.notes)

def test_three_five_stars_spaced_with_enough_fillers():  # #7335
    # No current five-star: slots 0, 4, 8 are exactly the spaced positions.
    inp = guard_input(incoming=[1, 2, 3] + list(range(10, 16)), ratings={1: 5, 2: 5, 3: 5})
    result = apply_guard(inp)
    assert sorted(result.kept) == sorted(inp.incoming)
    assert result.dropped == []
    assert_five_spacing(result.kept, inp.ratings)

def test_three_five_stars_with_current_five_keep_all_when_fillers_short():  # #7335
    # A five-star current track leaves only slots 4 and 8: the third is deferred, never dropped.
    inp = guard_input(current_id=90, incoming=[1, 2, 3] + list(range(10, 16)),
                      ratings={90: 5, 1: 5, 2: 5, 3: 5})
    result = apply_guard(inp)
    assert sorted(result.kept) == sorted(inp.incoming)
    assert result.dropped == []
    assert result.moved

def test_five_star_respects_fixed_queue_tail():
    inp = guard_input(
        upcoming=[90, 91, 92, 93], incoming=[1, 10], ratings={90: 5, 1: 5},
    )
    result = apply_guard(inp)
    combined = inp.upcoming + result.kept
    assert sorted(result.kept) == [1, 10]
    assert combined.index(1) - combined.index(90) >= 4
    assert_five_spacing(combined, inp.ratings)

def test_five_stars_are_deferred_never_dropped_when_fillers_run_out():
    result = apply_guard(guard_input(
        incoming=[1, 2, 3], ratings={1: 5, 2: 5, 3: 5},
        titles={1: "A5", 2: "B5", 3: "C5"},
    ))
    assert result.kept == [1, 2, 3]
    assert result.dropped == []
    assert result.moved == [2, 3]
    assert any("moved" in n and "keep five-stars" in n for n in result.notes)

@pytest.mark.parametrize("upcoming", [[], [91]])
def test_play_next_spaces_five_star_after_current_and_before_tail(upcoming):
    inp = guard_input(
        op="play_next", current_id=90, upcoming=upcoming,
        incoming=[1] + list(range(10, 17)), ratings={90: 5, 91: 5, 1: 5},
    )
    result = apply_guard(inp)
    assert sorted(result.kept) == sorted(inp.incoming)
    sequence = [inp.current_id] + result.kept + inp.upcoming
    assert sequence.index(1) >= 4
    assert_five_spacing(sequence, inp.ratings)

@pytest.mark.parametrize("source, expected", [
    ("todd", False), ("todd/karen:dj_queue", True), ("dj_pool:top_up", True),
    ("dj_triggers:cue", True), ("scheduled_stop:x", True),
    (None, False), ("server", False),
])
def test_is_agent_source(source, expected):
    assert is_agent_source(source) is expected

def test_todd_source_is_unguarded():
    # The bus bypasses apply_guard for phone/Todd sends; no bus is used here.
    assert is_agent_source("todd") is False

def test_single_explicit_play_now_ignores_cooldown():
    result = apply_guard(guard_input(
        op="play_now", incoming=[1], plays=[Play(1, NOW - 3600, "complete", 300)],
    ))
    assert result.kept == [1]
    assert result.notes == result.dropped == result.moved == []

def test_nonpositive_stream_ids_pass_through_without_deduplication():
    incoming = [-1, 0, -1, 0]
    result = apply_guard(guard_input(
        incoming=incoming, current_id=-1, upcoming=[0, -1],
        plays=[Play(-1, NOW - 3600, "complete", 300)],
    ))
    assert result.kept == incoming
    assert result.notes == result.dropped == result.moved == []

def test_work_blocked_keys_retains_base_and_applies_cooldown_window():
    work = {tid: work_key(f"Song {tid}", "Test Artist") for tid in range(1, 6)}
    base = {work_key("Base", "Test Artist")}
    plays = [
        Play(1, NOW - 3600, "complete", 0), Play(2, NOW - 86399, "skip", 0),
        Play(3, NOW - 86400, "complete", 300),
        Play(4, NOW - 90000, "complete", 300),
    ]
    assert work_blocked_keys(work, plays, NOW, base) == base | {work[1], work[2]}
    assert base == {work_key("Base", "Test Artist")}
    assert work_blocked_keys(work, [Play(5, NOW - 3600, "start", 300)], NOW) == set()

def test_load_guard_input_loads_counted_plays_and_real_work_keys():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool
    from audiplex.models import Album, Artist, Base, PlayStat, Track

    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    try:
        Base.metadata.create_all(bind=engine)
        with Session(engine) as db:
            artist = Artist(name="Ozzy Osbourne")
            album = Album(title="Test Album", artist=artist, folder_path="/fake/album")
            studio = Track(title="Driven To Tears", artist=artist, album=album,
                           file_path="/fake/studio.mp3")
            live = Track(title="Driven To Tears (Live)", artist=artist, album=album,
                         file_path="/fake/live.mp3")
            db.add_all([artist, album, studio, live])
            db.flush()
            at = NOW - 3600
            db.add_all([
                PlayStat(track_id=studio.id, event="complete", played_seconds=180,
                         timestamp=datetime.fromtimestamp(at, timezone.utc).replace(tzinfo=None)),
                PlayStat(track_id=live.id, event="start", played_seconds=0,
                         timestamp=datetime.fromtimestamp(NOW - 600, timezone.utc).replace(tzinfo=None)),
            ])
            db.commit()
            inp = load_guard_input(
                db, op="queue", incoming=[studio.id, live.id], current_id=None,
                upcoming=[], reserved=[], owner_id=None, now=NOW,
            )
            assert inp.plays == [Play(studio.id, at, "complete", 180.0)]
            assert inp.work[studio.id] == inp.work[live.id] == work_key(
                "Driven To Tears", "Ozzy Osbourne",
            )
            assert inp.titles == {studio.id: studio.title, live.id: live.title}
    finally:
        engine.dispose()


# --- integration: the bus choke point and the pool top-up (#7335) ---

def test_bus_guards_agent_batch_and_returns_notes(monkeypatch):  # #7335
    from audiplex import dj_pool, playback_bus, queue_guard as qg
    bus = playback_bus.PlaybackBus(seq_start=0)
    monkeypatch.setattr(playback_bus, "_pool_session", lambda: _FakeDb())
    monkeypatch.setattr(dj_pool, "owner_user_id", lambda db: None)
    monkeypatch.setattr(qg, "load_guard_input", lambda db, **kw: GuardInput(
        op=kw["op"], incoming=kw["incoming"], current_id=None, upcoming=[], reserved=[],
        plays=[], ratings={}, work={1: "work:a|x", 2: "work:a|x", 3: "work:b|y"},
        titles={1: "Song X", 2: "Song X (Live)", 3: "Song Y"}, now=NOW))
    rec = bus._enqueue("queue", {"track_ids": [1, 2, 3]}, source="todd/karen:dj_queue")
    assert rec.payload["track_ids"] == [1, 3]
    assert any("Song X (Live)" in n for n in rec.guard_notes)
    assert bus._commands[rec.id].guard_notes == rec.guard_notes


def test_bus_leaves_phone_and_todd_sends_untouched(monkeypatch):  # #7335
    from audiplex import playback_bus
    bus = playback_bus.PlaybackBus(seq_start=0)
    monkeypatch.setattr(playback_bus, "_pool_session", lambda: pytest.fail("guard ran on a Todd send"))
    rec = bus._enqueue("queue", {"track_ids": [1, 2]}, source="todd")
    assert rec.payload["track_ids"] == [1, 2]
    assert rec.guard_notes == []


def test_bus_all_dropped_queues_nothing(monkeypatch):  # #7335
    from audiplex import dj_pool, playback_bus, queue_guard as qg
    bus = playback_bus.PlaybackBus(seq_start=0)
    monkeypatch.setattr(playback_bus, "_pool_session", lambda: _FakeDb())
    monkeypatch.setattr(dj_pool, "owner_user_id", lambda db: None)
    monkeypatch.setattr(qg, "load_guard_input", lambda db, **kw: GuardInput(
        op=kw["op"], incoming=kw["incoming"], current_id=1, upcoming=[], reserved=[],
        plays=[], ratings={}, work={1: "work:a|x"}, titles={1: "Song X"}, now=NOW))
    rec = bus._enqueue("queue", {"track_ids": [1]}, source="dj_pool:top_up")
    assert rec.id == 0
    assert bus._commands == {}
    assert rec.guard_notes and "playing now" in rec.guard_notes[0]


class _FakeDb:
    def close(self):
        pass


def _pool_with_identities(monkeypatch, tmp_path, work_by_track):
    from audiplex import dj_pool
    from audiplex.identity import TrackIdentity
    monkeypatch.setattr(dj_pool, "build_identity_map", lambda db: {
        tid: TrackIdentity(track_id=tid, recording_id=f"rec:{tid}", work_id=w)
        for tid, w in work_by_track.items()})
    pool = dj_pool.DJPool(state_file=str(tmp_path / "pool.json"))
    pool.set_pool(spec_id=1, track_ids=list(work_by_track),
                  source_labels={tid: "A" for tid in work_by_track}, ahead=4)
    return pool


def test_pool_top_up_never_picks_two_versions_of_one_song(monkeypatch, tmp_path):  # #7335
    pool = _pool_with_identities(monkeypatch, tmp_path, {10: "work:a|x", 20: "work:a|x", 30: "work:b|y"})
    for _ in range(20):
        pool.state["played_this_session"] = []
        result = pool.top_up(current_track_id=1, upcoming_track_ids=[1], db=_FakeDb())
        picks = result["picks"]
        assert not ({10, 20} <= set(picks))


def test_pool_rejected_picks_are_not_consumed(monkeypatch, tmp_path):  # #7335
    pool = _pool_with_identities(monkeypatch, tmp_path, {10: "work:a|x", 20: "work:a|x", 30: "work:b|y"})
    # 20 shares 10's song and 10 is already queued: 20 must be rejected, never counted as played.
    result = pool.top_up(current_track_id=1, upcoming_track_ids=[1, 10], db=_FakeDb())
    assert 20 not in result["picks"]
    played = sum(lane.get("played_count", 0) for lane in pool.state["lanes"].values())
    assert played == len(result["picks"])
    assert 20 not in pool.state.get("played_this_session", [])

