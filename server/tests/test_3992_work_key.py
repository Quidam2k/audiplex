"""#3992: versions of one song share a work_key, including the forms #7335 missed.

Every pair below came from the live library (2026-10-09 audit). The negatives are
the false merges a looser rule produced in that audit.
"""

import pytest

from audiplex.identity import work_key
from audiplex.queue_guard import GuardInput, apply_guard

SAME_SONG = [
    ("King Princess",
     "“Fantastic” (from Arcane Season 2)",
     "Fantastic (from Arcane Season 2) | Live From Vevo Studios"),
    ("Thutmose", "Memories", "Memories | Spider-Man: Into the Spider-Verse OST"),
    ("Flogging Molly", "May the Living Be Dead (In Our Wake)", "May The Living Be Dead (In Our"),
    ("Kate Bush", "Running Up That Hill", "Running Up That Hill (A Deal W"),
    ("House of Pain", "Jump Around", "Jump Around [Cypress Hill Remi"),
    ("Kate Bush", "Cloudbusting", "Cloudbusting - Official Music Video"),
    ("Blondie", "Call Me", "Call Me - From American Gigolo"),
    ("The Police", "Driven To Tears", "Driven To Tears (Live)"),
]

DIFFERENT_SONGS = [
    ("League of Legends",
     "K/DA - VILLAIN ft. Madison Beer and Kim Petras (Official Concept Video - Starring Evelynn)",
     "K/DA - DRUM GO DUM ft. Aluna, Wolftyla, Bekuh BOOM"),
    ("Medeski Martin & Wood",
     "Medeski Martin & Wood - Notes From The Underground 2 The Saint",
     "Medeski Martin & Wood - Notes From The Underground 3 La Garonne"),
    ("The Ventures",
     "The Ventures - (05) - Theme From Silver City",
     "The Ventures - (07) - Theme From A Summer Place"),
    ("Simon & Garfunkel", "Scarborough Fair", "Scarborough Fair / Canticle"),
    ("Thing", "Life - A Portrait", "Life"),
]


@pytest.mark.parametrize("artist,a,b", SAME_SONG)
def test_versions_share_a_work_key(artist, a, b):
    assert work_key(a, artist) == work_key(b, artist)


@pytest.mark.parametrize("artist,a,b", DIFFERENT_SONGS)
def test_different_songs_keep_distinct_work_keys(artist, a, b):
    assert work_key(a, artist) != work_key(b, artist)


def test_guard_sends_one_fantastic_from_a_batch_holding_both():
    titles = {1: SAME_SONG[0][1], 2: SAME_SONG[0][2]}
    result = apply_guard(GuardInput(
        op="queue", incoming=[1, 2], current_id=None, upcoming=[], reserved=[],
        plays=[], ratings={}, titles=titles, now=1_000_000.0,
        work={tid: work_key(t, "King Princess") for tid, t in titles.items()},
    ))
    assert result.kept == [1]
    assert result.dropped == [2]
