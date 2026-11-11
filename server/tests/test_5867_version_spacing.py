"""#5867: several versions of one song in a bucket are never played back to back.

Todd, 11/04: keep all three versions in the bucket, but with more time between
them. The pool already enforces this through the work cooldown (#7335, 24 h) and
the queued/playing check; these tests pin it so it cannot regress.
"""
import time
from types import SimpleNamespace

from audiplex import queue_guard
from tests.test_5488_song_per_set import _FakeDb, _pool

# 10/11/12 are three versions of one work (live, remaster, original); 30/40/50 are others.
_VERSIONS = {
    **{tid: ("work:band|one song", f"song:one{tid}") for tid in (10, 11, 12)},
    **{tid: (f"work:band|other{tid}", f"song:other{tid}") for tid in (30, 40, 50)},
}


def _no_two_versions(picks):
    return len({10, 11, 12} & set(picks)) <= 1


def test_queued_version_blocks_its_other_versions(monkeypatch, tmp_path):
    pool = _pool(monkeypatch, tmp_path, _VERSIONS)
    for _ in range(40):
        pool.state["played_this_session"] = []
        picks = pool.top_up(current_track_id=1, upcoming_track_ids=[1, 10], db=_FakeDb())["picks"]
        assert not ({11, 12} & set(picks))


def test_one_top_up_never_holds_two_versions(monkeypatch, tmp_path):
    pool = _pool(monkeypatch, tmp_path, _VERSIONS)
    for _ in range(40):
        pool.state["played_this_session"] = []
        picks = pool.top_up(current_track_id=1, upcoming_track_ids=[1], db=_FakeDb())["picks"]
        assert _no_two_versions(picks), picks


def test_a_recent_play_keeps_every_version_out_for_a_day(monkeypatch, tmp_path):
    pool = _pool(monkeypatch, tmp_path, _VERSIONS)
    now = time.time()

    def guard_input(plays_at):
        return lambda *a, **k: SimpleNamespace(
            work={tid: w for tid, (w, _) in _VERSIONS.items()},
            plays=[queue_guard.Play(track_id=10, at=plays_at, event="complete", played_seconds=200.0)],
        )

    monkeypatch.setattr(queue_guard, "load_guard_input", guard_input(now - 3 * 3600))  # 3 h ago
    for _ in range(40):
        pool.state["played_this_session"] = []
        picks = pool.top_up(current_track_id=1, upcoming_track_ids=[1], db=_FakeDb())["picks"]
        assert not ({10, 11, 12} & set(picks)), picks

    monkeypatch.setattr(queue_guard, "load_guard_input", guard_input(now - 25 * 3600))  # a day later
    seen = set()
    for _ in range(60):
        pool.state["played_this_session"] = []
        seen |= {10, 11, 12} & set(pool.top_up(current_track_id=1, upcoming_track_ids=[1], db=_FakeDb())["picks"])
    assert seen
