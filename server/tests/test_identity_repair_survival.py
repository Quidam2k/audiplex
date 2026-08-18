"""The three verified work-level merges must survive tag repair (#3037).

#9265 ran the identity map over the live 217-track catalog and found exactly
three work-level merges, all correct and zero false. Retagging moves identity
keys — `work_id` is `normalize(artist) | strip_qualifiers(title)` and repair
changes both halves — so those three merges are the regression surface, and
they are pinned here with the library's real values.

The pairs are also what settled the precedence rule. Each one's uploading
channels disagree or are wrong while its title-parsed artist is identical, so a
channel-first ladder breaks two outright and gives the third an artist that does
not exist. Both directions are asserted below: repair keeps the merges, and the
rejected alternative would have destroyed them.
"""

import pytest

from audiplex.identity import build_identity_map, group_ids_by_recording, group_ids_by_work
from audiplex.models import Album, Artist, Track
from audiplex.tag_repair import propose

# (title tag, channel tag, duration) — verbatim from Todd's library.
BURY_A_FRIEND = [
    ("Billie Eilish - bury a friend (Lyrics)", "SyrebralVibes", 193.5),
    ("Billie Eilish - bury a friend (Official Music Video)", "BillieEilishVEVO", 212.1),
]
WE_KNOW_THE_WAY = [
    ("Lin-Manuel Miranda, Opetaia Foa'i - We Know The Way", "DisneyMusicVEVO", 141.4),
    (
        'Lin-Manuel Miranda, Opetaia Foa\'i - We Know The Way (From "Moana")',
        "DisneyMusicVEVO",
        155.6,
    ),
]
CONCRETE_WALL = [
    ("Zee Avi - Concrete Wall (Pumpkin Remix)", "LavelaL", 293.2),
    ("Zee Avi - Concrete Wall (Pumpkin Remix) [Free]", "Proximity Chill", 293.3),
]
ALL_PAIRS = [BURY_A_FRIEND, WE_KNOW_THE_WAY, CONCRETE_WALL]

YT = "https://www.youtube.com/watch?v=HUHC9tYz8ik"


def _album(db):
    artist = db.query(Artist).filter(Artist.name == "Various Artists").first()
    if artist is None:
        artist = Artist(name="Various Artists")
        db.add(artist)
        db.flush()
    album = db.query(Album).filter(Album.folder_path == "/music").first()
    if album is None:
        album = Album(title="music", artist_id=artist.id, folder_path="/music")
        db.add(album)
        db.flush()
    return album, artist


def _add(db, *, title, artist_name, duration, path):
    """One track, artist row created on demand. No file_hash: the YouTube rips
    are all distinct files, so hash-merging never fires in this library."""
    album, fallback = _album(db)
    if artist_name:
        artist = db.query(Artist).filter(Artist.name == artist_name).first()
        if artist is None:
            artist = Artist(name=artist_name)
            db.add(artist)
            db.flush()
    else:
        artist = fallback
    track = Track(
        title=title,
        album_id=album.id,
        artist_id=artist.id,
        duration_seconds=duration,
        file_path=path,
    )
    db.add(track)
    db.flush()
    return track


def _load_as_ripped(db):
    """The catalog as the scanner left it: artist blank, artist inside title."""
    ids = []
    for index, (title, _channel, duration) in enumerate(
        [row for pair in ALL_PAIRS for row in pair]
    ):
        ids.append(_add(db, title=title, artist_name=None, duration=duration,
                        path=f"/music/as_ripped_{index}.m4a").id)
    return ids


def _load_repaired(db):
    """The same catalog after the repair ladder has run over it."""
    ids = []
    for index, (title, channel, duration) in enumerate(
        [row for pair in ALL_PAIRS for row in pair]
    ):
        result = propose(title_tag=title, artist_tag=channel, comment=YT)
        assert result.auto_applicable, f"{title!r} should repair at high confidence"
        ids.append(_add(db, title=result.title, artist_name=result.artist,
                        duration=duration, path=f"/music/repaired_{index}.m4a").id)
    return ids


def _work_of(db, track_ids):
    identities = build_identity_map(db)
    return [identities[t].work_id for t in track_ids]


class TestTheThreeMergesSurvive:
    @pytest.mark.parametrize(
        "pair,name",
        [
            (BURY_A_FRIEND, "bury a friend"),
            (WE_KNOW_THE_WAY, "We Know The Way"),
            (CONCRETE_WALL, "Concrete Wall"),
        ],
    )
    def test_pair_shares_one_work_key_after_repair(self, db_session, pair, name):
        ids = []
        for index, (title, channel, duration) in enumerate(pair):
            result = propose(title_tag=title, artist_tag=channel, comment=YT)
            ids.append(
                _add(db_session, title=result.title, artist_name=result.artist,
                     duration=duration, path=f"/music/{name}_{index}.m4a").id
            )
        first, second = _work_of(db_session, ids)
        assert first == second, f"{name} must stay one work after repair"

    def test_merge_count_is_unchanged_across_the_repair(self, db_session):
        """Three merges before, three merges after. Not two, and not four —
        repair must not invent work-level merges either."""
        before_ids = _load_as_ripped(db_session)
        before = _work_of(db_session, before_ids)
        assert len(set(before)) == 3

        after_ids = _load_repaired(db_session)
        after = _work_of(db_session, after_ids)
        assert len(set(after)) == 3

    def test_no_two_different_songs_collapse_together(self, db_session):
        ids = _load_repaired(db_session)
        works = _work_of(db_session, ids)
        groups = {}
        for track_id, work in zip(ids, works):
            groups.setdefault(work, []).append(track_id)
        assert sorted(len(v) for v in groups.values()) == [2, 2, 2]


class TestPrecedenceRuleIsLoadBearing:
    def test_channel_first_would_have_broken_the_merges(self, db_session):
        """The counterfactual, asserted so nobody 'simplifies' the ladder later.

        Believe the channel instead of the title and bury a friend splits
        (SyrebralVibes vs BillieEilishVEVO), Concrete Wall splits (LavelaL vs
        Proximity Chill), and We Know The Way survives only under the name of a
        record label that never sang anything.
        """
        from audiplex.tag_repair import read_channel

        for pair in (BURY_A_FRIEND, CONCRETE_WALL):
            channels = {read_channel(channel).artist for _t, channel, _d in pair}
            assert len(channels) == 2, "channels disagree — a channel-first key splits"

        label = read_channel(WE_KNOW_THE_WAY[0][1]).artist
        assert label == "Disney Music"
        parsed = propose(
            title_tag=WE_KNOW_THE_WAY[0][0], artist_tag=WE_KNOW_THE_WAY[0][1], comment=YT
        ).artist
        assert parsed == "Lin-Manuel Miranda, Opetaia Foa'i"
        assert parsed != label


class TestConcreteWallNewlyPoolsItsHistory:
    """Signed off in plan-back #9272: the pair is two rips of ONE recording,
    293.2s and 293.3s apart. Once `[Free]` is stripped both titles normalize
    identically, so they newly merge at RECORDING level and pool their play
    history. Correct topology for duplicate rips — but a change, so it is
    asserted rather than left to be discovered."""

    def test_pair_is_one_recording_after_repair(self, db_session):
        ids = []
        for index, (title, channel, duration) in enumerate(CONCRETE_WALL):
            result = propose(title_tag=title, artist_tag=channel, comment=YT)
            ids.append(
                _add(db_session, title=result.title, artist_name=result.artist,
                     duration=duration, path=f"/music/cw_{index}.m4a").id
            )
        identities = build_identity_map(db_session)
        assert identities[ids[0]].recording_id == identities[ids[1]].recording_id
        assert len(group_ids_by_recording(identities)) == 1

    def test_the_other_two_pairs_stay_separate_recordings(self, db_session):
        """A shared work is not a shared rating. bury a friend's two rips are
        18 seconds apart and must keep their own opinions."""
        ids = []
        for index, (title, channel, duration) in enumerate(BURY_A_FRIEND):
            result = propose(title_tag=title, artist_tag=channel, comment=YT)
            ids.append(
                _add(db_session, title=result.title, artist_name=result.artist,
                     duration=duration, path=f"/music/baf_{index}.m4a").id
            )
        identities = build_identity_map(db_session)
        assert identities[ids[0]].recording_id != identities[ids[1]].recording_id
        assert identities[ids[0]].work_id == identities[ids[1]].work_id
