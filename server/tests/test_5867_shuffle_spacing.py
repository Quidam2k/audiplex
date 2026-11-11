"""#5867: versions of one song never land close together in a phone shuffle."""
import random

from audiplex.shuffle_spacing import ShuffleItem, gap_seconds, spaced_shuffle

SONG = "w-song"


def _items(uniques=40, versions=3, dur=240.0):
    items = [ShuffleItem(i, f"w{i}", dur) for i in range(uniques)]
    items += [ShuffleItem(1000 + i, SONG, dur) for i in range(versions)]
    return items


def test_every_seed_keeps_versions_at_least_the_gap_apart():
    items = _items()
    gap = gap_seconds(items)
    assert gap == 3600.0
    durations = {it.track_id: it.duration for it in items}
    for seed in range(50):
        order = spaced_shuffle(items, random.Random(seed))
        assert sorted(order) == sorted(durations)
        starts = [i for i, t in enumerate(order) if t >= 1000]
        for a, b in zip(starts, starts[1:]):
            between = sum(durations[t] for t in order[a + 1:b])
            assert between >= gap - 0.001, (seed, order)


def test_gap_scales_with_the_list_and_floor_zero_disables():
    big = _items(uniques=100, versions=2, dur=300.0)  # 31200 s over 2 versions -> 15600
    assert gap_seconds(big) == 0.5 * 102 * 300.0 / 2
    assert gap_seconds(big, floor=0) == 0.0
    assert gap_seconds([]) == 3600.0


def test_a_list_of_nothing_but_versions_still_plays_everything():
    items = [ShuffleItem(i, SONG, 200.0) for i in range(3)]
    assert sorted(spaced_shuffle(items, random.Random(1))) == [0, 1, 2]


def test_empty_and_floor_zero():
    assert spaced_shuffle([]) == []
    items = _items()
    assert sorted(spaced_shuffle(items, random.Random(2), floor=0)) == sorted(i.track_id for i in items)


def test_same_seed_same_order_and_missing_durations_do_not_break():
    items = _items() + [ShuffleItem(5000, "w-none", 0.0)]
    assert spaced_shuffle(items, random.Random(7)) == spaced_shuffle(items, random.Random(7))


def test_endpoint_returns_the_same_tracks_with_versions_apart(client, db_engine):  # #5867
    from sqlalchemy.orm import sessionmaker

    from audiplex.models import Album, Artist, Track

    session = sessionmaker(bind=db_engine)()
    artist = Artist(name="Band")
    session.add(artist)
    session.flush()
    album = Album(title="A", artist_id=artist.id, folder_path="/m/Band/A")
    session.add(album)
    session.flush()
    titles = ["Hey Song", "Hey Song (Live)", "Hey Song - Remastered"] + [f"Other {i}" for i in range(40)]
    ids = []
    for n, title in enumerate(titles):
        t = Track(title=title, album_id=album.id, artist_id=artist.id, disc_number=1, track_number=n,
                  duration_seconds=240.0 + n, file_path=f"/m/Band/A/{n}.mp3")
        session.add(t)
        session.flush()
        ids.append(t.id)
    session.commit()
    session.close()

    resp = client.post("/api/music/shuffle-order", json={"track_ids": ids + [999999]})
    assert resp.status_code == 200
    order = resp.json()["track_ids"]
    assert sorted(order) == sorted(ids + [999999])
    hey = [i for i, t in enumerate(order) if t in ids[:3]]
    assert len(hey) == 3
    assert all(b - a >= 10 for a, b in zip(hey, hey[1:])), hey  # 3600 s at ~250 s a song is 14+ tracks
