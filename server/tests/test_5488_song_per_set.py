"""Set-scoped song preferences and song-key normalization (#5488)."""

import random
from types import SimpleNamespace

import pytest

from audiplex import dj_bans, dj_context_picks, dj_learn, dj_pool, queue_guard
from audiplex.identity import TrackIdentity, song_key


def test_cover_titles_share_song_key():  # #5488
    versions = [
        ("Late Night Jazz - CD02 - 02 - Peggy Lee - It Ain't Necessarily So", "Peggy Lee"),
        ("oscar peterson - 15 - It Ain't Necessarily So", "Oscar Peterson"),
        ("It Ain't Necessarily So - (duet with Ella Fritzgerald - Aug 18, 1957  Los Angeles, California)",
         "Louis Armstrong"),
        ("It Ain't Necessarily So", "Ella Fitzgerald & Louis Armstr"),
    ]
    assert {song_key(title, artist) for title, artist in versions} == {
        song_key("It Ain't Necessarily So", "Peggy Lee")
    }


def test_composition_and_numbered_parts_remain_distinct():  # #5488
    assert song_key("Symphony No 1 in Eb KV16 - Andante", "W A Mozart") != song_key(
        "Andante", "Beethoven"
    )
    assert song_key("Moby Dick - 01", "Laurie Anderson") != song_key(
        "Moby Dick - 02", "Laurie Anderson"
    )


def test_known_trailing_artist_is_removed():  # #5488
    assert song_key("bury a friend - Billie Eilish", "", frozenset({"billie eilish"})) == (
        song_key("bury a friend", "Billie Eilish")
    )


def test_one_word_library_artist_preserves_title_segment():  # #5488
    assert song_key("Symphony No 1 - Allegro", "Mozart", frozenset({"allegro"})) != (
        song_key("Symphony No 1", "Mozart")
    )


def test_version_tail_is_removed_across_artists():  # #5488
    assert song_key("Hurt - Live", "Johnny Cash") == song_key("Hurt", "Nine Inch Nails")


@pytest.mark.parametrize("title, artist", [("Track 3", "Unknown Artist"), ("AudioTrack 05", "x"), ("", "x")])
def test_placeholders_have_no_song_key(title, artist):  # #5488
    assert song_key(title, artist) == ""


class _FakeDb:
    def close(self):
        pass


def _pool(monkeypatch, tmp_path, idents: dict[int, tuple[str, str]], lanes: dict[int, str] | None = None):
    monkeypatch.setattr(dj_pool, "build_identity_map", lambda db: {
        tid: TrackIdentity(track_id=tid, recording_id=f"rec:{tid}", work_id=work, song_id=song)
        for tid, (work, song) in idents.items()
    })
    monkeypatch.setattr(dj_pool, "owner_user_id", lambda db: None)
    monkeypatch.setattr(dj_bans, "banned_ids", lambda *args: set())
    monkeypatch.setattr(dj_learn, "weights_for", lambda db: {})
    monkeypatch.setattr(queue_guard, "load_guard_input",
                        lambda *args, **kwargs: SimpleNamespace(work={}, plays=[]))
    monkeypatch.setattr(dj_pool, "_context_tilt", lambda: dj_context_picks.ContextTilt(False))
    rng = random.Random(5488)
    monkeypatch.setattr(dj_pool, "_weighted_pick", lambda ids, weights: rng.choice(ids))
    pool = dj_pool.DJPool(state_file=str(tmp_path / "pool.json"))
    pool.set_pool(spec_id=1, track_ids=list(idents),
                  source_labels=lanes or {tid: "A" for tid in idents}, ahead=4)
    return pool


_HURT = {
    10: ("work:nin|hurt", "song:hurt"),
    20: ("work:cash|hurt", "song:hurt"),
    **{tid: (f"work:artist|song{tid}", f"song:song{tid}") for tid in (30, 40, 50)},
}


def test_queued_cover_blocks_other_artist_in_set(monkeypatch, tmp_path):  # #5488
    pool = _pool(monkeypatch, tmp_path, _HURT)
    for _ in range(30):
        pool.state["played_this_session"] = []
        picks = pool.top_up(current_track_id=1, upcoming_track_ids=[1, 10], db=_FakeDb())["picks"]
        assert len(picks) == 3
        assert set(picks) == {30, 40, 50}


def test_one_top_up_does_not_repeat_song(monkeypatch, tmp_path):  # #5488
    pool = _pool(monkeypatch, tmp_path, {**_HURT, 60: ("work:x|extra", "song:extra")})
    for _ in range(30):
        pool.state["played_this_session"] = []
        picks = pool.top_up(current_track_id=1, upcoming_track_ids=[1], db=_FakeDb())["picks"]
        assert len(picks) == 4
        assert not {10, 20}.issubset(picks)


def test_earlier_session_play_blocks_cover(monkeypatch, tmp_path):  # #5488
    pool = _pool(monkeypatch, tmp_path, _HURT)
    # Three fresh songs fill this request; a fourth slot would relax the preference.
    pool.state["ahead"] = 3
    pool.state["played_this_session"] = [10]
    picks = pool.top_up(current_track_id=1, upcoming_track_ids=[1], db=_FakeDb())["picks"]
    assert set(picks) == {30, 40, 50}
    assert 20 not in picks


def test_cover_is_allowed_in_next_set(monkeypatch, tmp_path):  # #5488
    pool = _pool(monkeypatch, tmp_path, _HURT)
    pool.state["played_this_session"] = [10]
    pool.set_pool(spec_id=1, track_ids=list(_HURT),
                  source_labels={tid: "A" for tid in _HURT}, ahead=4)
    assert pool.state["played_this_session"] == []
    seen = False
    for _ in range(50):
        pool.state["played_this_session"] = []
        picks = pool.top_up(current_track_id=1, upcoming_track_ids=[1], db=_FakeDb())["picks"]
        if 20 in picks:
            seen = True
            break
    assert seen


def test_small_bucket_relaxes_song_preference(monkeypatch, tmp_path):  # #5488
    pool = _pool(monkeypatch, tmp_path, {tid: _HURT[tid] for tid in (10, 20)})
    pool.state["played_this_session"] = [10]
    picks = pool.top_up(current_track_id=1, upcoming_track_ids=[1], db=_FakeDb())["picks"]
    assert picks == [20]


def test_generic_title_preference_is_set_scoped(monkeypatch, tmp_path):  # #5488
    idents = {10: ("work:a|home", "song:home"), 20: ("work:b|home", "song:home"),
              30: ("work:c|other", "song:other")}
    pool = _pool(monkeypatch, tmp_path, idents)
    pool.state["ahead"] = 2  # One vacancy with Home queued, filled by the distinct song.
    assert pool.top_up(current_track_id=1, upcoming_track_ids=[1, 10], db=_FakeDb())["picks"] == [30]
    pool.set_pool(spec_id=1, track_ids=list(idents),
                  source_labels={tid: "A" for tid in idents}, ahead=2)
    assert pool.state["played_this_session"] == []
    seen = False
    for _ in range(50):
        pool.state["played_this_session"] = []
        if 20 in pool.top_up(current_track_id=1, upcoming_track_ids=[1], db=_FakeDb())["picks"]:
            seen = True
            break
    assert seen


def test_artist_prefix_comes_off_before_version_tails():  # #5488
    assert song_key("Wings - Live and Let Die", "Wings") == song_key("Live and Let Die", "Guns N' Roses")
    assert song_key("Wings - Live and Let Die", "Wings") != song_key("Wings - Single Pigeon", "Wings")


def test_four_digit_lead_is_a_name_not_a_track_number():  # #5488
    assert song_key("1812 - Overture", "Tchaikovsky") != song_key("2112 - Overture", "Rush")
    assert song_key("07 - Overture", "x") == song_key("Overture", "y")


def test_song_heard_before_its_top_up_still_blocks_its_cover(monkeypatch, tmp_path):  # #5488
    pool = _pool(monkeypatch, tmp_path, _HURT)
    pool.state["ahead"] = 1
    # 10 plays when the set starts (not a pool pick); the queue is full, so nothing is picked.
    pool.top_up(current_track_id=10, upcoming_track_ids=[10, 30], db=_FakeDb())
    # Later 10 is gone from the reported queue; its cover must still wait.
    for _ in range(20):
        pool.state["played_this_session"] = [30]
        picks = pool.top_up(current_track_id=30, upcoming_track_ids=[30], db=_FakeDb())["picks"]
        assert picks and 20 not in picks
