"""#3984 song memory (audiplex_mcp/song_context.py + the MCP queue-time hook).

Scratch audiplex / RFL-notes / buckets DBs only. Proves: reasons are stored
evidence, another artist's notes never leak in, "never after" vetoes, bans /
cooldown / weak-only links are dropped, theme search is whole-word and one
entry per song, Todd's love words and rating notes never reach any output,
and the queue hook is fail-open and time-bounded.
"""

import asyncio
import importlib.util
import json
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from audiplex_mcp import song_context as sc  # noqa: E402

RFL_STORE = Path("Q:/Development/radio_free_luna/src/notes/store.py")
pytestmark = pytest.mark.skipif(not RFL_STORE.is_file(), reason="needs the RFL notes store module for song_key")

SECRET_LOVE = "reminds me of my dad's kitchen"
SECRET_RATING = "therapy song do not air"
NOW = datetime(2026, 10, 12, 20, 0, tzinfo=timezone.utc)

# id: (title, artist)
TRACKS = {
    1: ("Seed Song", "Alpha"),
    2: ("Sampled Tune", "Beta"),       # sampled by Seed Song
    3: ("Writer Twin", "Gamma"),       # same writer as Seed Song, other act
    4: ("Alpha Again", "Alpha"),       # same act + same writer: not news
    5: ("Year Mate", "Delta"),         # only the release year: weak, dropped
    6: ("Banned One", "Epsilon"),      # sampled by Seed too, but banned
    7: ("Banned One", "Epsilon"),      # second copy of the banned recording
    8: ("Played Lately", "Zeta"),      # shares a writer, played 2 h ago
    9: ("Never Next", "Eta"),          # pair note says never
    10: ("Seed Song", "Somebody Else"),  # same title, other artist: no Alpha facts
    11: ("War Cry", "Theta"),
    12: ("War Cry", "Theta"),          # duplicate copy for theme dedupe
    13: ("Warmonger", "Iota"),         # 'war' only as a prefix: no whole-word match
}

RFL_FACTS = {
    ("Alpha", "Seed Song"): [
        ("summary", '"Seed Song" is a song by Alpha about leaving home.'),
        ("samples", 'The track samples "Sampled Tune" by Beta and "Banned One" by Epsilon.'),
        ("writers", "Written by Jane Writer and Alpha."),
        ("release", "First released in 1999."),
        ("theme", "The lyrics describe a lighthouse keeper waiting for dawn."),
    ],
    ("Beta", "Sampled Tune"): [("summary", '"Sampled Tune" is an instrumental by Beta.')],
    ("Gamma", "Writer Twin"): [
        ("writers", "Written by Jane Writer."),
        ("theme", "A song about a lighthouse keeper and the sea."),
    ],
    ("Alpha", "Alpha Again"): [("writers", "Written by Jane Writer and Alpha.")],
    ("Delta", "Year Mate"): [("release", "First released in 1999.")],
    ("Epsilon", "Banned One"): [("writers", "Written by Jane Writer.")],
    ("Zeta", "Played Lately"): [("writers", "Written by Jane Writer.")],
    ("Eta", "Never Next"): [("writers", "Written by Jane Writer.")],
    ("Theta", "War Cry"): [("theme", "An anti-war anthem from the Vietnam years.")],
    ("Iota", "Warmonger"): [("theme", "Warmongers and their profits.")],
}


def _load_store():
    spec = importlib.util.spec_from_file_location("rfl_store_for_3984_test", RFL_STORE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def dbs(tmp_path, monkeypatch):
    store = _load_store()
    aud, rfl, buckets = tmp_path / "audiplex.db", tmp_path / "dj_notes.db", tmp_path / "buckets.db"
    with sqlite3.connect(aud) as c:
        c.executescript("""
            CREATE TABLE artists(id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE tracks(id INTEGER PRIMARY KEY, title TEXT, artist_id INT, content_kind TEXT,
                                bpm REAL, musical_key TEXT, energy REAL);
            CREATE TABLE track_ratings(id INTEGER PRIMARY KEY, track_id INT, user_id INT, rating REAL,
                                       note TEXT, updated_at TEXT);
            CREATE TABLE dj_pair_notes(id INTEGER PRIMARY KEY, track_a INT, track_b INT, note TEXT,
                                       persona TEXT, created_at TEXT);
            CREATE TABLE dj_bans(track_id INT, reason TEXT, persona TEXT, created_at TEXT);
            CREATE TABLE dj_track_tags(track_id INT, tag TEXT, persona TEXT, created_at TEXT);
            CREATE TABLE play_stats(id INTEGER PRIMARY KEY, track_id INT, event TEXT, played_seconds REAL,
                                    timestamp TEXT, user_id INT);
        """)
        artists = {}
        for tid, (title, artist) in TRACKS.items():
            aid = artists.setdefault(artist, len(artists) + 1)
            c.execute("INSERT OR IGNORE INTO artists VALUES (?, ?)", (aid, artist))
            c.execute("INSERT INTO tracks(id, title, artist_id, content_kind) VALUES (?, ?, ?, 'music')",
                      (tid, title, aid))
        c.execute("INSERT INTO track_ratings(track_id, user_id, rating, note, updated_at) VALUES (1, 1, 4.5, ?, '2026-10-01')",
                  (f"[Juno, said 4.5] {SECRET_RATING}",))
        c.executemany("INSERT INTO dj_pair_notes(track_a, track_b, note, persona, created_at) VALUES (?, ?, ?, 'jarvis', ?)", [
            (1, None, f"{sc.LOVE_PREFIX}: [private] {SECRET_LOVE}", "2026-10-02"),
            (1, None, f"{sc.LOVE_PREFIX}: {SECRET_LOVE}", "2026-10-03"),
            (1, 9, "never after Seed Song, it kills the mood", "2026-10-04"),
            (1, 2, "great lift into the climb", "2026-10-05"),
        ])
        c.execute("INSERT INTO dj_bans VALUES (6, 'Todd hates it', 'jarvis', '2026-10-01')")
        c.executemany("INSERT INTO dj_track_tags VALUES (?, ?, 'jarvis', '2026-10-01')",
                      [(1, "loved"), (1, "five-star-verbal"), (11, "protest")])
        c.execute("INSERT INTO play_stats(track_id, event, played_seconds, timestamp, user_id) VALUES (8, 'complete', 200, ?, 1)",
                  ((NOW - timedelta(hours=2)).isoformat(),))
    with sqlite3.connect(rfl) as c:
        c.executescript("""
            CREATE TABLE songs(key TEXT PRIMARY KEY, artist TEXT, title TEXT, title_key TEXT, mb_recording TEXT,
                               mb_work TEXT, wikidata TEXT, wiki_url TEXT, status TEXT, detail TEXT, fetched_at TEXT);
            CREATE TABLE facts(id INTEGER PRIMARY KEY AUTOINCREMENT, song_key TEXT, kind TEXT, text TEXT,
                               source_name TEXT, source_url TEXT, rank INT DEFAULT 0, origin TEXT DEFAULT 'auto');
        """)
        for (artist, title), facts in RFL_FACTS.items():
            key = store.song_key(artist, title)
            c.execute("INSERT INTO songs(key, artist, title, title_key, status, fetched_at) VALUES (?, ?, ?, ?, 'ok', 'x')",
                      (key, artist, title, store.norm_title(title, artist)))
            for rank, (kind, text) in enumerate(facts):
                c.execute("INSERT INTO facts(song_key, kind, text, source_name, source_url, rank) VALUES (?, ?, ?, 'Wikipedia', ?, ?)",
                          (key, kind, text, f"https://en.wikipedia.org/wiki/{title.replace(' ', '_')}", rank))
    with sqlite3.connect(buckets) as c:
        c.execute("CREATE TABLE buckets(id INTEGER PRIMARY KEY, name TEXT)")
        c.execute("CREATE TABLE bucket_tracks(bucket_id INT, track_id INT, path TEXT, position INT)")
        c.execute("INSERT INTO buckets VALUES (1, 'Lighthouse Nights')")
        c.executemany("INSERT INTO bucket_tracks VALUES (1, ?, '', ?)", [(1, 0), (3, 1)])
    monkeypatch.setenv("AUDIPLEX_DB_PATH", str(aud))
    monkeypatch.setenv("RFL_DJ_NOTES_DB", str(rfl))
    monkeypatch.setenv("RFL_STORE_PY", str(RFL_STORE))
    monkeypatch.setenv("DJ_BUCKETS_DB", str(buckets))
    sc._reset_cache()
    yield
    sc._reset_cache()


def _kinds(entry):
    return {r["kind"] for r in entry["reasons"]}


def test_context_is_sourced_and_private(dbs):
    ctx = sc.context(1)
    assert ctx["title"] == "Seed Song" and ctx["artist"] == "Alpha"
    assert ctx["facts"][0]["text"] == '"Seed Song" is a song by Alpha about leaving home.'
    assert all(f["source"] == "Wikipedia" and f["url"].startswith("https://") for f in ctx["facts"])
    assert ctx["todd"] == {"stars": 4.5, "loved": True, "tags": []}  # rating/love tags are flags, not tags
    blob = json.dumps(ctx)
    assert SECRET_LOVE not in blob and SECRET_RATING not in blob and sc.LOVE_PREFIX not in blob


def test_another_artists_song_gets_no_notes(dbs):
    assert sc.context(10)["facts"] == []  # same title, different artist: exact key only


def test_after_reasons_are_stored_evidence(dbs):
    picks = {p["track_id"]: p for p in sc.after(1, n=10, now=NOW)}
    assert {"pair_note", "sample_link"} <= _kinds(picks[2])
    sample = next(r for r in picks[2]["reasons"] if r["kind"] == "sample_link")
    assert sample["text"] == RFL_FACTS[("Alpha", "Seed Song")][1][1]  # verbatim
    assert {"same_writer", "shared_theme", "shared_bucket"} <= _kinds(picks[3])
    theme = next(r for r in picks[3]["reasons"] if r["kind"] == "shared_theme")
    assert theme["match"] == "keeper lighthouse" or theme["match"] == "lighthouse keeper"
    assert picks[2]["score"] > picks[3]["score"]  # Todd's own pair note outranks inference


def test_after_drops_veto_bans_cooldown_weak_and_self(dbs):
    ids = {p["track_id"] for p in sc.after(1, n=25, now=NOW)}
    assert 9 not in ids   # "never after" pair note vetoes
    assert not ids & {6, 7}  # banned, and its second copy
    assert 8 not in ids   # played 2 h ago
    assert 5 not in ids   # only shares a release year
    assert 4 not in ids   # same act sharing its own writer is not a link
    assert 1 not in ids
    later = {p["track_id"] for p in sc.after(1, n=25, now=NOW + timedelta(hours=13))}
    assert 8 in later     # cooldown expires
    assert 3 not in {p["track_id"] for p in sc.after(1, n=25, exclude_ids=[3], now=NOW)}


def test_link_veto_is_the_only_reason(dbs):
    assert [r["kind"] for r in sc.link(1, 9)] == ["veto"]


def test_theme_whole_word_one_entry_per_song(dbs):
    hits = sc.theme("war")
    assert [h["track_id"] for h in hits] == [11]  # 11 and 12 are one song; 'Warmonger' is not 'war'
    assert hits[0]["why"]["text"] == "An anti-war anthem from the Vietnam years."
    assert [h["track_id"] for h in sc.theme("protest")] == [11]  # tag match
    assert {h["track_id"] for h in sc.theme("lighthouse")} >= {1, 3}  # bucket name
    assert sc.theme("ab") == []


def test_queue_context_block(dbs):
    block = sc.queue_context([1, 2, 9])
    assert block.startswith("\nCONTEXT (sourced; weave in, never add to it):")
    assert "Seed Song - Alpha:" in block and "(Wikipedia)" in block
    assert "Seed Song -> Sampled Tune: great lift into the climb" in block
    assert "CAUTION Sampled Tune -> Never Next" not in block  # only neighbours with a stored link
    assert SECRET_LOVE not in block and SECRET_RATING not in block
    assert "CAUTION Seed Song -> Never Next" in sc.queue_context([1, 9])
    assert len(sc.queue_context(list(TRACKS))) <= 1200


def test_everything_fails_open_without_the_notes(dbs, monkeypatch):
    monkeypatch.setenv("AUDIPLEX_DB_PATH", str(Path(sc.__file__).parent / "no_such.db"))
    sc._reset_cache()
    assert sc.context(1) is None and sc.after(1) == [] and sc.theme("war") == [] and sc.queue_context([1]) == ""


# --- the MCP side: tools and the queue-time hook --------------------------------------------

def _server():
    from audiplex_mcp import server
    return server


def test_tools_never_show_love_words(dbs):
    server = _server()
    out = asyncio.run(server.dj_song_context(1)) + asyncio.run(server.dj_plays_well_after(1))
    assert "Seed Song" in out and SECRET_LOVE not in out and SECRET_RATING not in out
    assert "lighthouse" in asyncio.run(server.dj_theme_songs("lighthouse")).lower()


def test_hook_appends_only_for_what_was_queued(dbs):
    server = _server()
    note = asyncio.run(server._song_context_note({"id": 7, "kept": 2}, [1, 2]))
    assert "Seed Song -> Sampled Tune" in note and SECRET_LOVE not in note
    assert asyncio.run(server._song_context_note("held: Todd is talking", [1, 2])) == ""
    assert asyncio.run(server._song_context_note({"id": None}, [1, 2])) == ""
    assert asyncio.run(server._song_context_note({"id": 7, "kept": 1}, [1, 2])) == ""  # guard dropped one
    only = asyncio.run(server._song_context_note({"id": 7, "dropped_missing": [2]}, [1, 2]))
    assert "- Sampled Tune - Beta" not in only and "- Seed Song - Alpha" in only


def test_hook_never_delays_a_queue_call(dbs, monkeypatch):
    server = _server()
    monkeypatch.setattr(server.song_context, "queue_context", lambda ids: time.sleep(3) or "late")
    monkeypatch.setattr(server, "SONG_CONTEXT_TIMEOUT_S", 0.2)
    async def timed():  # time the await itself: asyncio.run's shutdown joins the stray thread, a live server doesn't
        start = time.monotonic()
        note = await server._song_context_note({"id": 7}, [1])
        return note, time.monotonic() - start

    note, took = asyncio.run(timed())
    assert note == "" and took < 1.0
