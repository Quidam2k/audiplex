"""#5696: never two songs by the same act back to back in a DJ pool.

11/07: todd-ride-mix has a Cirque du Soleil lane AND Cirque tracks in other lanes,
spelled three ways. The rule folds spellings, drops "feat.", exempts compilations,
holds at the join with the queue end, and never starves a one-artist pool.
"""
import random
from types import SimpleNamespace

import pytest

from audiplex import dj_pool
from audiplex.identity import artist_key

CUR = 9000


def _pool(tmp_path, lanes, ahead):
    pool = dj_pool.DJPool(state_file=tmp_path / "pool.json")
    pool.set_pool(
        1,
        [t for ids in lanes.values() for t in ids],
        {},
        lanes=dj_pool.lanes_from_ids(lanes),
        ahead=ahead,
        exclude_recent_hours=0,
    )
    return pool


@pytest.fixture
def identities(monkeypatch):
    """Return install(artists) -> fake db, with identities for every track id."""

    def install(artists):
        ids = {
            t: SimpleNamespace(
                recording_id=f"r{t}",
                work_id=f"w{t}",
                song_id=f"s{t}",
                artist_key=artist_key(name),
            )
            for t, name in artists.items()
        }
        monkeypatch.setattr(dj_pool, "build_identity_map", lambda db: ids)
        monkeypatch.setattr(dj_pool, "owner_user_id", lambda db: None)
        monkeypatch.setattr("audiplex.dj_bans.banned_ids", lambda db, identities=None: [])
        tilt = SimpleNamespace(
            weights=lambda seed, candidates, weights: (weights, {}),
            enabled=False,
            gave_up=0,
        )
        monkeypatch.setattr(dj_pool, "_context_tilt", lambda *a, **k: tilt)
        monkeypatch.setattr("audiplex.dj_learn.weights_for", lambda db: {})
        return object()

    return install


def artists_in_order(picks, artists):
    return [artist_key(artists[t]) for t in picks]


def no_repeats(seq):
    return all(a != b or not a for a, b in zip(seq, seq[1:]))


def _run(pool, db):
    return pool.top_up(
        current_track_id=CUR,
        upcoming_track_ids=[CUR],
        current_played_track_ids=[CUR],
        db=db,
    )


def test_cirque_spellings_never_adjacent(tmp_path, identities):
    """#5696: spelling variants of one artist are never queued back to back."""
    spellings = ["Cirque du Soleil", "Cirque Du Soleil", "CIRQUE DU SOLEIL"]
    artists = {CUR: "Radiohead"}
    for n, t in enumerate(range(1, 7)):
        artists[t] = spellings[n % 3]
    for t in (11, 12, 13):
        artists[t] = "CIRQUE DU SOLEIL"
    artists.update({14: "Sting", 15: "Adele", 16: "Bjork"})
    lanes = {"cirque": list(range(1, 7)), "mix": list(range(11, 17))}
    db = identities(artists)

    for i in range(20):
        random.seed(i)
        sub = tmp_path / f"seed{i}"
        sub.mkdir()
        r = _run(_pool(sub, lanes, 7), db)  # 3 others: C,O,C,O,C,O,C is the most without a repeat
        seq = artists_in_order([CUR] + list(r["picks"]), artists)
        assert no_repeats(seq), f"seed {i}: {seq}"


def test_join_with_queue_end(tmp_path, identities):
    """#5696: the first pick must not repeat the artist of the current track."""
    artists = {CUR: "Cirque du Soleil", 1: "Cirque du Soleil", 2: "Cirque Du Soleil",
               3: "CIRQUE DU SOLEIL", 11: "Sting", 12: "Adele"}
    db = identities(artists)
    lanes = {"cirque": [1, 2, 3], "other": [11, 12]}
    r = _run(_pool(tmp_path, lanes, 1), db)

    assert r["picks"]
    assert r["picks"][0] in (11, 12)
    assert any("artist rule" in s for s in r["context_reasons"])


def test_one_artist_pool_still_fills(tmp_path, identities):
    """#5696: a single-artist pool is still filled despite the artist rule."""
    artists = {CUR: "Santana", **{t: "Santana" for t in range(1, 6)}}
    db = identities(artists)
    r = _run(_pool(tmp_path, {"only": [1, 2, 3, 4, 5]}, 4), db)

    assert len(r["picks"]) == 4


def test_various_artists_not_blocked(tmp_path, identities):
    """#5696: compilation 'Various Artists' tracks are exempt from the artist rule."""
    artists = {CUR: "Various Artists", **{t: "Various Artists" for t in range(1, 5)}}
    db = identities(artists)
    r = _run(_pool(tmp_path, {"comp": [1, 2, 3, 4]}, 3), db)

    assert len(r["picks"]) == 3
    assert not any("artist rule" in s for s in r["context_reasons"])


def test_featured_counts_as_lead(tmp_path, identities):
    """#5696: 'X feat. Y' counts as artist X for the back-to-back rule."""
    artists = {CUR: "Santana", 1: "Santana feat. Rob Thomas", 11: "Sting"}
    db = identities(artists)
    r = _run(_pool(tmp_path, {"a": [1], "b": [11]}, 1), db)

    assert r["picks"] == [11]


def test_artist_key_normalisation():
    """#5696: artist_key folds case, featured credits and compilation names."""
    assert (
        artist_key("Cirque du Soleil")
        == artist_key("Cirque Du Soleil")
        == artist_key("CIRQUE DU SOLEIL")
    )
    assert artist_key("Santana feat. Rob Thomas") == artist_key("Santana") == "santana"
    assert artist_key("Various Artists") == ""
    assert artist_key("Ft. Knox") != ""
    assert artist_key("Ft. Knox")
