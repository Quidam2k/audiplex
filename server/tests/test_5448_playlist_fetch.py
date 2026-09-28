"""#5448 dj_fetch_from_playlists: standing-approval fetch from Todd's own allowlisted playlists."""
import asyncio
import contextlib
import json
import re
import sqlite3
import sys
import types
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from audiplex_mcp import playlist_fetch


class FakeMCP:
    def __init__(self):
        self.registered = None

    def tool(self):
        def decorator(function):
            self.registered = function
            return function

        return decorator


def _match_key(*parts):
    words = re.findall(r"[a-z0-9]+", " ".join(parts).lower())
    return " ".join(sorted(words))


def _split_artist_title(raw, uploader=""):
    parts = re.split(r"\s+-\s+", raw, maxsplit=1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return uploader.strip(), raw.strip()


def _fmt_duration(seconds):
    if not isinstance(seconds, (int, float)) or seconds <= 0:
        return "?:??"
    total = int(seconds)
    minutes, secs = divmod(total, 60)
    return f"{minutes}:{secs:02d}"


def _make_context(
    tmp_path,
    monkeypatch,
    entries,
    library=None,
):
    allowlist_path = tmp_path / "yt_playlists.json"
    cache_path = tmp_path / "yt_playlist_cache.json"
    destination = tmp_path / "music"
    database = tmp_path / "taste.db"
    staging = tmp_path / "staging"

    allowlist_path.write_text(
        json.dumps(
            {
                "playlists": [
                    {
                        "id": "PL_ALLOWLISTED",
                        "name": "Todd music",
                        "dest": str(destination),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("DJ_YT_PLAYLISTS_FILE", str(allowlist_path))
    monkeypatch.setenv("DJ_YT_PLAYLIST_CACHE", str(cache_path))

    connection = sqlite3.connect(database)
    connection.execute(
        """CREATE TABLE candidates (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at    TEXT NOT NULL,
        query         TEXT NOT NULL DEFAULT '',
        url           TEXT NOT NULL,
        title         TEXT NOT NULL DEFAULT '',
        artist        TEXT NOT NULL DEFAULT '',
        duration      REAL,
        rec_id        INTEGER,
        approval      TEXT NOT NULL DEFAULT '',
        ingested_at   TEXT,
        ingested_path TEXT NOT NULL DEFAULT ''
        )"""
    )
    connection.commit()
    connection.close()

    @contextlib.contextmanager
    def taste_db():
        conn = sqlite3.connect(database)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    state = {
        "downloads": 0,
        "rescans": 0,
        "list_calls": 0,
        "library": list(library or []),
    }

    def download_audio(url, dest_dir):
        state["downloads"] += 1
        path = Path(dest_dir) / "dl.m4a"
        path.write_bytes(b"audio")
        return path, {"duration": 200}

    async def all_music_tracks():
        return list(state["library"])

    def list_playlist(playlist_id):
        state["list_calls"] += 1
        assert playlist_id == "PL_ALLOWLISTED"
        return [dict(entry) for entry in entries]

    async def rescan():
        state["rescans"] += 1
        return {"added": 1, "updated": 0, "removed": 0}

    ns = {
        "_download_audio": download_audio,
        "_write_tags": lambda path, title, artist, album, year: None,
        "_split_artist_title": _split_artist_title,
        "_safe_name": lambda name, fallback="Unknown": name or fallback,
        "_match_key": _match_key,
        "_all_music_tracks": all_music_tracks,
        "_taste_db": taste_db,
        "_now": lambda: "2026-09-28T12:00:00",
        "_plain": lambda exc: str(exc),
        "_fmt_duration": _fmt_duration,
        "_headers": lambda: {"Authorization": "Bearer test"},
        "AUDIPLEX_URL": "http://audiplex.test",
        "STAGING_DIR": staging,
        "LONGFORM_SECONDS": 900,
        "_require": lambda module, package: __import__(module),
    }

    playlist_fetch._VIDEO_LOCKS.clear()
    monkeypatch.setattr(playlist_fetch, "_list_playlist", list_playlist)
    monkeypatch.setattr(playlist_fetch, "_rescan", rescan)

    mcp = FakeMCP()
    playlist_fetch.register(mcp, ns)

    return {
        "fn": mcp.registered,
        "state": state,
        "database": database,
        "destination": destination,
        "cache": cache_path,
    }


def _entry(video_id="vid001", title="Artist - Unique Song", duration=200):
    return {
        "video_id": video_id,
        "title": title,
        "uploader": "Artist",
        "duration": duration,
    }


def test_unique_match_downloads_records_approval_and_rescans(
    tmp_path,
    monkeypatch,
):
    context = _make_context(tmp_path, monkeypatch, [_entry()])

    result = asyncio.run(context["fn"](query="unique song"))

    expected = context["destination"] / "Artist - Unique Song.m4a"
    assert expected.read_bytes() == b"audio"
    assert context["state"]["downloads"] == 1
    assert context["state"]["rescans"] == 1
    assert str(expected) in result
    assert "standing #3245" in result

    connection = sqlite3.connect(context["database"])
    connection.row_factory = sqlite3.Row
    row = connection.execute("SELECT * FROM candidates").fetchone()
    connection.close()

    assert row["url"] == "https://www.youtube.com/watch?v=vid001"
    assert row["title"] == "Unique Song"
    assert row["artist"] == "Artist"
    assert "standing #3245" in row["approval"]
    assert "PL_ALLOWLISTED" in row["approval"]
    assert row["ingested_path"] == str(expected)
    assert row["ingested_at"]


def test_zero_match_refuses_without_downloading(tmp_path, monkeypatch):
    context = _make_context(tmp_path, monkeypatch, [_entry()])

    result = asyncio.run(context["fn"](query="something absent"))

    assert "not in Todd's allowlisted playlists" in result
    assert "dj_find_candidates" in result
    assert context["state"]["downloads"] == 0
    assert context["state"]["rescans"] == 0


def test_multiple_matches_lists_ids_without_downloading(tmp_path, monkeypatch):
    entries = [
        _entry("first-id", "Artist One - Shared Song"),
        _entry("second-id", "Artist Two - Shared Song"),
    ]
    context = _make_context(tmp_path, monkeypatch, entries)

    result = asyncio.run(context["fn"](query="shared song"))

    assert "first-id | Artist One - Shared Song | 3:20 | Todd music" in result
    assert "second-id | Artist Two - Shared Song | 3:20 | Todd music" in result
    assert "Call again with video_id" in result
    assert context["state"]["downloads"] == 0


def test_video_id_outside_allowlisted_playlist_is_refused(
    tmp_path,
    monkeypatch,
):
    context = _make_context(tmp_path, monkeypatch, [_entry("allowed-id")])

    result = asyncio.run(context["fn"](video_id="not-allowed"))

    assert "not in Todd's allowlisted playlists" in result
    assert "dj_find_candidates" in result
    assert "dj_ingest" in result
    assert context["state"]["downloads"] == 0


def test_second_call_returns_already_fetched_without_redownload(
    tmp_path,
    monkeypatch,
):
    context = _make_context(tmp_path, monkeypatch, [_entry()])

    first = asyncio.run(context["fn"](query="unique song"))
    second = asyncio.run(context["fn"](video_id="vid001"))

    assert "Fetched:" in first
    assert "Already fetched:" in second
    assert context["state"]["downloads"] == 1
    assert context["state"]["rescans"] == 1


def test_library_duplicate_is_refused(tmp_path, monkeypatch):
    context = _make_context(
        tmp_path,
        monkeypatch,
        [_entry()],
        library=[{"title": "Artist - Unique Song"}],
    )

    result = asyncio.run(context["fn"](query="unique song"))

    assert "library already has it" in result
    assert "dj_search" in result
    assert context["state"]["downloads"] == 0


def test_longform_is_refused_without_flag(tmp_path, monkeypatch):
    context = _make_context(
        tmp_path,
        monkeypatch,
        [_entry(duration=901)],
    )

    result = asyncio.run(context["fn"](query="unique song"))

    assert "Refusing long-form" in result
    assert "allow_longform=True" in result
    assert context["state"]["downloads"] == 0


def test_cache_reused_within_ttl_and_refresh_relists(tmp_path, monkeypatch):
    context = _make_context(tmp_path, monkeypatch, [_entry()])

    asyncio.run(context["fn"](query="unique song"))
    asyncio.run(context["fn"](video_id="vid001"))
    assert context["state"]["list_calls"] == 1

    asyncio.run(context["fn"](video_id="vid001", refresh=True))
    assert context["state"]["list_calls"] == 2
    assert context["cache"].exists()


def test_list_playlist_passes_no_cookie_options(tmp_path, monkeypatch):
    captured = {}

    class FakeYoutubeDL:
        def __init__(self, options):
            captured.update(options)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def extract_info(self, url, download=False):
            assert url == (
                "https://www.youtube.com/playlist?list=PL_ALLOWLISTED"
            )
            assert download is False
            return {
                "entries": [
                    {
                        "id": "vid001",
                        "title": "Artist - Song",
                        "uploader": "Artist",
                        "duration": 200,
                    }
                ]
            }

    fake_yt_dlp = types.ModuleType("yt_dlp")
    fake_yt_dlp.YoutubeDL = FakeYoutubeDL
    monkeypatch.setitem(sys.modules, "yt_dlp", fake_yt_dlp)

    mcp = FakeMCP()
    playlist_fetch.register(
        mcp,
        {"_require": lambda module, package: __import__(module)},
    )

    result = playlist_fetch._list_playlist("PL_ALLOWLISTED")

    assert result == [
        {
            "video_id": "vid001",
            "title": "Artist - Song",
            "uploader": "Artist",
            "duration": 200,
        }
    ]
    assert captured["extract_flat"] is True
    assert captured["skip_download"] is True
    assert captured["quiet"] is True
    assert captured["logtostderr"] is True
    assert captured["noprogress"] is True
    assert captured["socket_timeout"] == 30
    assert not any("cookie" in key.lower() for key in captured)


def test_library_duplicate_by_artist_and_title(tmp_path, monkeypatch):
    context = _make_context(
        tmp_path,
        monkeypatch,
        [_entry()],
        library=[{"title": "Unique Song", "artist_name": "Artist"}],
    )

    result = asyncio.run(context["fn"](query="unique song"))

    assert "library already has it" in result
    assert context["state"]["downloads"] == 0
