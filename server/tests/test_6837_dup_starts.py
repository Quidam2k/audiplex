"""#6837: duplicate 'start' play stats (extra phone MediaControllers) are counted once."""
from datetime import datetime, timedelta, timezone

from audiplex import taste
from audiplex.dj_priority import recent_sequence
from audiplex.models import PlayStat, Track, User
from audiplex.play_dedupe import collapse_dup_starts
from audiplex.routers.music import likely_skips_for


def _collapse(items):
    return collapse_dup_starts(items, lambda x: x[0], lambda x: x[1], lambda x: x[2])


def test_collapse_keeps_one_of_a_run_and_leaves_other_events_alone():
    items = [(1, "start", 0.0), (1, "start", 1.0), (1, "start", 2.5), (1, "start", 5.4),
             (1, "complete", 2.0), (2, "start", 1.0), (1, "start", 100.0)]
    # chain 0 -> 1 -> 2.5 collapses (each within 3 s of the previous start); 5.4 is 2.9 s
    # after 2.5, so it is part of the chain too; 100 is a real replay
    assert _collapse(items) == [(1, "start", 0.0), (1, "complete", 2.0), (2, "start", 1.0), (1, "start", 100.0)]


def test_collapse_is_order_independent_and_keeps_input_order():
    items = [(1, "start", 2.0), (1, "start", 0.0), (2, "start", 0.5)]
    assert _collapse(items) == [(1, "start", 0.0), (2, "start", 0.5)] or \
        _collapse(items) == [(2, "start", 0.5), (1, "start", 0.0)]


def _owner(db):
    return db.query(User).filter(User.username == "testuser").first()


def _track(db, album, title):
    t = Track(title=title, album_id=album.id, artist_id=album.artist_id, file_path=f"/x/{title}.mp3", file_size=1)
    db.add(t)
    db.commit()
    return t


def test_recent_plays_feed_recent_sequence_once_per_play(db_session, sample_album):
    owner = _owner(db_session)
    a = _track(db_session, sample_album, "A")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    base = now - timedelta(minutes=10)
    db_session.add_all([PlayStat(track_id=a.id, user_id=owner.id, event="start",
                                 timestamp=base + timedelta(seconds=s)) for s in (0, 1, 2)])
    db_session.commit()
    plays = taste.recent_plays_for(db_session, owner.id, 60)
    assert recent_sequence(plays, 60, set()) == [a.id]


def test_recording_completion_rate_not_deflated(db_session, sample_album):
    owner = _owner(db_session)
    a = _track(db_session, sample_album, "A")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    base = now - timedelta(hours=1)
    db_session.add_all(
        [PlayStat(track_id=a.id, user_id=owner.id, event="start", timestamp=base + timedelta(seconds=s))
         for s in (0, 1, 2)]
        + [PlayStat(track_id=a.id, user_id=owner.id, event="complete", played_seconds=200,
                    timestamp=base + timedelta(minutes=3))])
    db_session.commit()
    stats = taste.recording_stats_for(db_session, owner.id)
    entry = next(e for e in stats.values() if a.id in e.track_ids)
    assert (entry.starts, entry.completes, entry.completion_rate) == (1, 1, 1.0)


def test_skip_suspect_starts_collapse_and_real_replays_count(db_session, sample_album):
    owner = _owner(db_session)
    a = _track(db_session, sample_album, "A")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    base = now - timedelta(hours=1)
    rows = [PlayStat(track_id=a.id, user_id=owner.id, event="start", timestamp=base + timedelta(seconds=s))
            for s in (0, 1, 2, 600)]  # one play posted three times, then a real second play
    rows += [PlayStat(track_id=a.id, user_id=owner.id, event="skip", played_seconds=2.0,
                      timestamp=base + timedelta(seconds=s)) for s in (3, 603)]
    db_session.add_all(rows)
    db_session.commit()
    out = likely_skips_for(db_session, owner.id, 10, 2)
    assert [(r.early_skip_count, r.total_starts) for r in out] == [(2, 2)]


def test_history_endpoint_collapses_and_still_fills_the_limit(client, db_session, sample_album, monkeypatch):
    from audiplex.routers import playback

    s = playback.get_settings().model_copy(update={"dj_owner_username": "testuser"})
    monkeypatch.setattr(playback, "get_settings", lambda: s)
    owner = _owner(db_session)
    a, b = _track(db_session, sample_album, "A"), _track(db_session, sample_album, "B")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    adds = []
    for i, tr in enumerate((a, b, a, b)):  # four plays, each posted three times
        t0 = now - timedelta(minutes=40 - 10 * i)
        adds += [PlayStat(track_id=tr.id, user_id=owner.id, event="start",
                          timestamp=t0 + timedelta(seconds=k)) for k in range(3)]
    db_session.add_all(adds)
    db_session.commit()
    rows = client.get("/api/playback/history?limit=3").json()
    assert [r["title"] for r in rows] == ["B", "A", "B"]
