"""Fetch tracks from Todd's own allowlisted YouTube playlists (#5448)."""

import asyncio
import datetime
import json
import os
import shutil
import tempfile
from pathlib import Path

import httpx


_NS: dict | None = None
_VIDEO_LOCKS: dict[str, asyncio.Lock] = {}
_CACHE_TTL_SECONDS = 6 * 60 * 60


def register(mcp, ns: dict) -> None:
    """Register the playlist-fetch tool without importing server.py."""
    global _NS
    _NS = ns
    mcp.tool()(dj_fetch_from_playlists)


def _helper(name: str):
    """Get a server helper at call time so tests can replace it."""
    if _NS is None:
        raise RuntimeError("playlist_fetch.register() has not been called")
    try:
        return _NS[name]
    except KeyError:
        raise RuntimeError(f"server helper {name} is unavailable") from None


def _ascii(value) -> str:
    # MCP results are UTF-8; keep real titles ("Eivor" with its accent) intact.
    return str(value)


def _result(message: str, notes: list[str] | None = None) -> str:
    lines = [message]
    lines.extend(notes or [])
    return _ascii("\n".join(lines))


def _plain(exc: Exception) -> str:
    try:
        return _helper("_plain")(exc)
    except Exception:
        return str(exc).strip()


def _allowlist_path() -> Path:
    configured = os.environ.get("DJ_YT_PLAYLISTS_FILE")
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parent / "yt_playlists.json"


def _cache_path() -> Path:
    configured = os.environ.get("DJ_YT_PLAYLIST_CACHE")
    if configured:
        return Path(configured)
    staging_dir = Path(_helper("STAGING_DIR"))
    return staging_dir.parent / "yt_playlist_cache.json"


def _load_allowlist() -> list[dict]:
    path = _allowlist_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(
            f"Could not read playlist allowlist {path}: {_plain(exc)}"
        ) from None

    playlists = raw.get("playlists") if isinstance(raw, dict) else None
    if not isinstance(playlists, list) or not playlists:
        raise RuntimeError(
            f"Invalid playlist allowlist {path}: expected a non-empty playlists list"
        )

    validated = []
    for index, item in enumerate(playlists, start=1):
        if not isinstance(item, dict):
            raise RuntimeError(
                f"Invalid playlist allowlist {path}: playlist {index} is not an object"
            )
        playlist_id = item.get("id")
        name = item.get("name")
        dest = item.get("dest")
        if not all(isinstance(value, str) and value.strip() for value in (
            playlist_id,
            name,
            dest,
        )):
            raise RuntimeError(
                f"Invalid playlist allowlist {path}: playlist {index} needs "
                "non-empty id, name, and dest strings"
            )
        validated.append(
            {
                "id": playlist_id.strip(),
                "name": name.strip(),
                "dest": dest.strip(),
            }
        )
    return validated


def _read_cache(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _cache_is_fresh(record: dict) -> bool:
    fetched_at = record.get("fetched_at")
    entries = record.get("entries")
    if not isinstance(fetched_at, str) or not isinstance(entries, list):
        return False
    try:
        stamp = datetime.datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=datetime.timezone.utc)
        age = datetime.datetime.now(datetime.timezone.utc) - stamp
    except (TypeError, ValueError, OverflowError):
        return False
    return 0 <= age.total_seconds() < _CACHE_TTL_SECONDS


def _write_cache(path: Path, cache: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(cache, handle, ensure_ascii=True, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    except Exception:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass
        raise


def _list_playlist(playlist_id: str) -> list[dict]:
    """Return flat metadata for one explicitly allowlisted playlist."""
    _helper("_require")("yt_dlp", "yt-dlp")
    from yt_dlp import YoutubeDL

    opts = {
        "extract_flat": True,
        "skip_download": True,
        "quiet": True,
        "logtostderr": True,
        "noprogress": True,
        "socket_timeout": 30,
    }
    url = f"https://www.youtube.com/playlist?list={playlist_id}"
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False) or {}

    result = []
    for entry in info.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        video_id = entry.get("id") or entry.get("video_id")
        if not isinstance(video_id, str) or not video_id:
            continue
        result.append(
            {
                "video_id": video_id,
                "title": entry.get("title") or "(untitled)",
                "uploader": (
                    entry.get("uploader")
                    or entry.get("channel")
                    or entry.get("channel_id")
                    or ""
                ),
                "duration": entry.get("duration"),
            }
        )
    return result


async def _playlist_entries(
    playlists: list[dict],
    refresh: bool,
) -> tuple[list[dict], list[str]]:
    cache_path = _cache_path()
    cache = _read_cache(cache_path)
    cache_changed = False
    notes: list[str] = []
    combined: list[dict] = []

    for playlist in playlists:
        playlist_id = playlist["id"]
        record = cache.get(playlist_id)
        if not isinstance(record, dict):
            record = {}
        cached_entries = record.get("entries")
        has_stale_entries = isinstance(cached_entries, list)

        if not refresh and _cache_is_fresh(record):
            entries = cached_entries
        else:
            try:
                entries = await asyncio.to_thread(_list_playlist, playlist_id)
                cache[playlist_id] = {
                    "fetched_at": datetime.datetime.now(
                        datetime.timezone.utc
                    ).isoformat(),
                    "entries": entries,
                }
                cache_changed = True
            except Exception as exc:
                if not has_stale_entries:
                    raise RuntimeError(
                        f"Could not list allowlisted playlist "
                        f"{playlist['name']} ({playlist_id}): {_plain(exc)}"
                    ) from None
                entries = cached_entries
                notes.append(
                    f"Using stale playlist cache for {playlist['name']} "
                    f"({playlist_id}) because refresh failed: {_plain(exc)}"
                )

        for entry in entries:
            if not isinstance(entry, dict):
                continue
            entry_video_id = entry.get("video_id")
            if not isinstance(entry_video_id, str) or not entry_video_id:
                continue
            combined.append(
                {
                    "video_id": entry_video_id,
                    "title": entry.get("title") or "(untitled)",
                    "uploader": entry.get("uploader") or "",
                    "duration": entry.get("duration"),
                    "playlist_id": playlist_id,
                    "playlist_name": playlist["name"],
                    "dest": playlist["dest"],
                }
            )

    if cache_changed:
        try:
            _write_cache(cache_path, cache)
        except Exception as exc:
            notes.append(f"Could not update playlist cache: {_plain(exc)}")

    return combined, notes


async def _rescan() -> dict:
    """Rescan music after a successful move into an allowlisted destination."""
    async with httpx.AsyncClient(timeout=180) as client:
        response = await client.post(
            f"{_helper('AUDIPLEX_URL')}/api/library/scan/music",
            headers=_helper("_headers")(),
        )
    response.raise_for_status()
    return response.json()


def _existing_destination(dest_dir: Path, base_name: str) -> Path | None:
    if not dest_dir.is_dir():
        return None
    prefix = f"{base_name}.".casefold()
    try:
        for path in dest_dir.iterdir():
            if path.is_file() and path.name.casefold().startswith(prefix):
                return path
    except OSError:
        return None
    return None


async def _already_fetched(video_id: str):
    with _helper("_taste_db")() as connection:
        return connection.execute(
            "SELECT ingested_at, ingested_path FROM candidates "
            "WHERE instr(url, ?) > 0 AND ingested_at IS NOT NULL "
            "ORDER BY id DESC LIMIT 1",
            (video_id,),
        ).fetchone()


async def _fetch_entry(entry: dict, query: str, notes: list[str]) -> str:
    video_id = entry["video_id"]
    lock = _VIDEO_LOCKS.setdefault(video_id, asyncio.Lock())

    async with lock:
        try:
            previous = await _already_fetched(video_id)
        except Exception as exc:
            return _result(
                f"Could not check prior playlist fetches: {_plain(exc)}",
                notes,
            )
        if previous:
            return _result(
                f"Already fetched: {previous['ingested_path']}",
                notes,
            )

        duration = entry.get("duration")
        longform_seconds = _helper("LONGFORM_SECONDS")
        if (
            not entry["allow_longform"]
            and isinstance(duration, (int, float))
            and duration >= longform_seconds
        ):
            return _result(
                f"Refusing long-form playlist entry "
                f"({_helper('_fmt_duration')(duration)}): {entry['title']}. "
                "Call again with allow_longform=True only if this is intentional.",
                notes,
            )

        split_artist_title = _helper("_split_artist_title")
        artist, title = split_artist_title(
            entry.get("title") or "",
            entry.get("uploader") or "",
        )
        artist = (artist or entry.get("uploader") or "Unknown").strip()
        title = (title or entry.get("title") or "").strip()
        if not title:
            return _result("Refusing: the playlist entry has no usable title.", notes)

        match_key = _helper("_match_key")
        try:
            library = await _helper("_all_music_tracks")()
        except Exception as exc:
            return _result(
                f"Could not check the library for duplicates: {_plain(exc)}. "
                "Nothing downloaded.",
                notes,
            )

        library_keys = set()
        for track in library:
            if not isinstance(track, dict):
                continue
            library_keys.add(match_key(track.get("title") or ""))
            if track.get("artist_name"):
                library_keys.add(
                    match_key(track["artist_name"], track.get("title") or "")
                )
        if match_key(artist, title) in library_keys or match_key(title) in library_keys:
            return _result(
                f"Refusing: library already has it: {artist} - {title}. "
                "Use dj_search to find the existing track.",
                notes,
            )

        safe_name = _helper("_safe_name")
        destination_dir = Path(entry["dest"])
        destination_base = safe_name(f"{artist} - {title}", "track")
        existing = _existing_destination(destination_dir, destination_base)
        if existing is not None:
            return _result(
                f"Refusing: destination file already exists: {existing}",
                notes,
            )

        staging = Path(_helper("STAGING_DIR")) / f"yt_{video_id}"
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, exist_ok=True)

        url = f"https://www.youtube.com/watch?v={video_id}"
        try:
            downloaded, info = await asyncio.to_thread(
                _helper("_download_audio"),
                url,
                staging,
            )
            downloaded = Path(downloaded)
            info = info or {}
        except Exception as exc:
            shutil.rmtree(staging, ignore_errors=True)
            return _result(
                f"Download failed, nothing added: {_plain(exc)}",
                notes,
            )

        real_duration = info.get("duration")
        if (
            not entry["allow_longform"]
            and isinstance(real_duration, (int, float))
            and real_duration >= longform_seconds
        ):
            shutil.rmtree(staging, ignore_errors=True)
            return _result(
                f"Discarded: downloaded file is "
                f"{_helper('_fmt_duration')(real_duration)}, which is long-form. "
                "Nothing was added. Pass allow_longform=True if intentional.",
                notes,
            )

        try:
            await asyncio.to_thread(
                _helper("_write_tags"),
                downloaded,
                title,
                artist,
                entry["playlist_name"],
                info.get("release_year"),
            )
        except Exception as exc:
            return _result(
                f"Tagging failed: {_plain(exc)}. File left in {staging}; "
                "nothing was added to the library.",
                notes,
            )

        suffix = downloaded.suffix
        destination = destination_dir / f"{destination_base}{suffix}"
        if destination.exists():
            return _result(
                f"Refusing: destination file already exists: {destination}. "
                f"Downloaded file left in {staging}.",
                notes,
            )

        try:
            destination_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(downloaded), str(destination))
        except OSError as exc:
            return _result(
                f"Could not move the track into {destination_dir}: {_plain(exc)}. "
                f"File left in {staging}.",
                notes,
            )
        shutil.rmtree(staging, ignore_errors=True)

        recorded_duration = (
            real_duration
            if isinstance(real_duration, (int, float))
            else duration
        )
        approval = (
            f"standing #3245: own playlist {entry['playlist_name']} "
            f"({entry['playlist_id']})"
        )
        now = _helper("_now")
        try:
            with _helper("_taste_db")() as connection:
                connection.execute(
                    "INSERT INTO candidates "
                    "(created_at, query, url, title, artist, duration, rec_id, "
                    "approval, ingested_at, ingested_path) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        now(),
                        query,
                        url,
                        title,
                        artist,
                        recorded_duration,
                        None,
                        approval,
                        now(),
                        str(destination),
                    ),
                )
        except Exception as exc:
            return _result(
                f"Track is on disk at {destination}, but provenance recording "
                f"failed: {_plain(exc)}",
                notes,
            )

        lines = [
            f"Fetched: {artist} - {title} "
            f"({_helper('_fmt_duration')(recorded_duration)})",
            f"Destination: {destination}",
            f"Approval: {approval}",
        ]
        try:
            scan = await _rescan()
            lines.append(
                f"Rescan: added={scan.get('added')} "
                f"updated={scan.get('updated')} removed={scan.get('removed')}"
            )
        except Exception as exc:
            lines.append(
                f"Rescan failed: {_plain(exc)}. The file is on disk but is not "
                "yet catalogued."
            )
        lines.extend(notes)
        return _ascii("\n".join(lines))


async def dj_fetch_from_playlists(
    query: str = "",
    video_id: str = "",
    refresh: bool = False,
    allow_longform: bool = False,
) -> str:
    """Fetch a track covered by Todd's standing approval #3245.

    The standing approval applies only to tracks found in Todd's own allowlisted
    YouTube playlists. A text query must have exactly one match before anything
    is downloaded; use video_id to resolve an ambiguous result. Anything outside
    those playlists must go through dj_find_candidates, Todd's explicit yes, and
    dj_ingest.
    """
    query = (query or "").strip()
    video_id = (video_id or "").strip()
    if not query and not video_id:
        return _result(
            "Give a query or an exact video_id from an allowlisted playlist."
        )

    try:
        playlists = _load_allowlist()
        entries, notes = await _playlist_entries(playlists, refresh)
    except Exception as exc:
        return _result(f"Playlist fetch unavailable: {_plain(exc)}")

    if video_id:
        matches = [entry for entry in entries if entry["video_id"] == video_id]
        if not matches:
            return _result(
                f"Refusing: video_id {video_id} is not in Todd's allowlisted "
                "playlists. Use dj_find_candidates, get Todd's yes, then use "
                "dj_ingest.",
                notes,
            )
        selected = matches[0]
    else:
        match_key = _helper("_match_key")
        query_words = match_key(query).split()
        if not query_words:
            return _result("Give a query containing at least one word.", notes)

        matches = []
        for entry in entries:
            title_words = set(match_key(entry.get("title") or "").split())
            if all(word in title_words for word in query_words):
                matches.append(entry)

        if not matches:
            return _result(
                f"'{query}' is not in Todd's allowlisted playlists. Use "
                "dj_find_candidates, get Todd's yes, then use dj_ingest.",
                notes,
            )
        if len(matches) > 1:
            lines = [
                f"Multiple allowlisted playlist matches for '{query}'. "
                "Call again with video_id:"
            ]
            for entry in matches[:10]:
                lines.append(
                    f"{entry['video_id']} | {entry['title']} | "
                    f"{_helper('_fmt_duration')(entry.get('duration'))} | "
                    f"{entry['playlist_name']}"
                )
            lines.extend(notes)
            return _ascii("\n".join(lines))
        selected = matches[0]

    selected = dict(selected)
    selected["allow_longform"] = allow_longform
    return await _fetch_entry(selected, query, notes)
