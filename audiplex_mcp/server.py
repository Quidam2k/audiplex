"""Audiplex DJ — dedicated MCP server.

Exposes the Audiplex playback command bus as agent ("DJ") tools. Kept as a
standalone package (NOT folded into pantheon_mcp_server) so other Pantheon
adopters can run it independently against their own Audiplex instance.

Config via environment:
  AUDIPLEX_URL    base URL of the Audiplex server (e.g. http://100.x.y.z:8000)
  AUDIPLEX_TOKEN  service-account JWT — mint via:
                    cd server && python -m audiplex.create_service_token
                  When unset/empty, falls back to reading a `.dj_token` file
                  at the repo root (single source: rotation = re-mint +
                  overwrite that one file instead of editing every agent's
                  MCP config).

  DJ_TTS_URL      OpenAI-compatible speech endpoint for voice breaks — see
                  tts_backend.py for the full TTS config surface. Only
                  dj_announce needs it; the other tools work without it.

Tools: dj_library, dj_tracks, dj_search, dj_play_now, dj_skip, dj_queue,
dj_play_next, dj_reorder, dj_queue_by, dj_now_playing, dj_pause, dj_resume,
dj_previous, dj_seek, dj_volume, dj_play_stream, dj_break_brief, dj_announce,
dj_recommend, dj_rate, dj_taste.

dj_recommend/dj_rate/dj_taste are the discovery + taste lane (item #2945): the
DJ proposes music the library DOESN'T have, Todd's spoken reaction is relayed
back through dj_rate, and dj_taste feeds the accumulated signal into later
picks. State lives in a local SQLite file (DJ_TASTE_DB, default
data/dj/taste.db) rather than audiplex.db — see the section comment above
dj_recommend for why the existing tables can't carry it.

dj_library/dj_tracks/dj_search are the catalog-browse lane (item #2943): they
let the agent survey the library and pick tracks UNPROMPTED instead of only
taking requests. They matter more than they look, because the live library is
a flat yt-dlp dump with no embedded tags — every track scanned into one album
under a nameless artist with no genres — so the artist/album/genre axes are
degenerate and only the track TITLES carry real information. dj_library says
so explicitly rather than letting an agent conclude the library is empty.
Browse is resolved MCP-side over the existing library-global catalog REST
(/api/music/folders, /folders/tracks, /artists, /albums, /genres, /roots);
there is still no server-side /search endpoint and none was added.

dj_break_brief and dj_announce are the DJ-persona lane (item #431):
dj_break_brief hands the agent a dayparted brief, the agent writes the copy,
dj_announce synthesizes and queues it as a voice break. The agent can pass
explicit track IDs or let dj_queue_by resolve an artist/album/genre/folder/
playlist/favorites NAME to tracks MCP-side. Playlist and favorites resolution go through /api/playback/
(owner-resolved reads), NOT /api/music/ — the latter is scoped to the
caller and dj-agent has none. dj_play_stream routes an external HTTP audio
stream (e.g. Radio Free Luna's /stream.mp3) to the device — see the
token-leak guard in the Android AuthInterceptor before assuming this is
safe to extend to other stream-carrying commands.
"""

import contextlib
import datetime
import os
import re
import sqlite3
from pathlib import Path
from urllib.parse import quote

import httpx
from mcp.server.fastmcp import FastMCP

from audiplex_mcp import dj_persona, tts_backend

AUDIPLEX_URL = os.environ.get("AUDIPLEX_URL", "http://localhost:8000").rstrip("/")


def _load_token() -> str:
    """Env wins; otherwise fall back to the .dj_token file at the repo root."""
    token = os.environ.get("AUDIPLEX_TOKEN", "")
    if token:
        return token
    token_file = Path(__file__).resolve().parent.parent / ".dj_token"
    try:
        return token_file.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


AUDIPLEX_TOKEN = _load_token()

mcp = FastMCP("audiplex-dj")


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {AUDIPLEX_TOKEN}"}


@mcp.tool()
async def dj_play_now(track_ids: list[int]) -> str:
    """Immediately play the given music tracks on the Audiplex device,
    replacing the current queue.

    track_ids are Audiplex music track IDs — resolve names/albums/artists to
    IDs first via the catalog REST API (GET /api/music/albums, /artists, etc.).
    Tracks play in the order given.
    """
    if not track_ids:
        return "No track_ids given; nothing to play."
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{AUDIPLEX_URL}/api/playback/command",
            headers=_headers(),
            json={"type": "play_now", "payload": {"track_ids": track_ids}},
        )
    if resp.status_code == 401:
        return "Auth failed (401). Check AUDIPLEX_TOKEN."
    resp.raise_for_status()
    data = resp.json()
    return (
        f"Queued play_now for {len(track_ids)} track(s) "
        f"(command #{data.get('id')}, {data.get('pending')} pending). "
        "The device plays when it next polls (immediately if awake)."
    )


@mcp.tool()
async def dj_skip() -> str:
    """Skip to the next track in the Audiplex device's current queue.

    No-op if nothing is queued after the current track. Use dj_now_playing
    afterward to confirm what's playing.
    """
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{AUDIPLEX_URL}/api/playback/command",
            headers=_headers(),
            json={"type": "skip", "payload": {}},
        )
    if resp.status_code == 401:
        return "Auth failed (401). Check AUDIPLEX_TOKEN."
    resp.raise_for_status()
    data = resp.json()
    return (
        f"Queued skip (command #{data.get('id')}, {data.get('pending')} pending). "
        "The device skips to the next track when it next polls."
    )


async def _enqueue(cmd_type: str, payload: dict) -> str:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{AUDIPLEX_URL}/api/playback/command",
            headers=_headers(),
            json={"type": cmd_type, "payload": payload},
        )
    if resp.status_code == 401:
        return "Auth failed (401). Check AUDIPLEX_TOKEN."
    resp.raise_for_status()
    return resp.json()


@mcp.tool()
async def dj_queue(track_ids: list[int]) -> str:
    """Append the given music tracks to the END of the device's current queue.

    If nothing is currently playing, this starts playback (same as dj_play_now).
    track_ids are Audiplex music track IDs, played in the order given.
    """
    if not track_ids:
        return "No track_ids given; nothing to queue."
    data = await _enqueue("queue", {"track_ids": track_ids})
    if isinstance(data, str):
        return data
    return (
        f"Queued {len(track_ids)} track(s) to the end "
        f"(command #{data.get('id')}, {data.get('pending')} pending). "
        "Appended when the device next polls."
    )


@mcp.tool()
async def dj_play_next(track_ids: list[int]) -> str:
    """Insert the given music tracks immediately AFTER the currently-playing
    track, so they play next without disturbing the rest of the queue.

    If nothing is currently playing, this starts playback (same as dj_play_now).
    """
    if not track_ids:
        return "No track_ids given; nothing to insert."
    data = await _enqueue("play_next", {"track_ids": track_ids})
    if isinstance(data, str):
        return data
    return (
        f"Inserted {len(track_ids)} track(s) to play next "
        f"(command #{data.get('id')}, {data.get('pending')} pending)."
    )


@mcp.tool()
async def dj_reorder(from_index: int, to_index: int) -> str:
    """Move a queued track from one position to another. Indices are 0-based
    positions in the current queue — read them from dj_now_playing, which lists
    the queue with its indices.
    """
    data = await _enqueue("reorder", {"from_index": from_index, "to_index": to_index})
    if isinstance(data, str):
        return data
    return (
        f"Queued reorder {from_index} -> {to_index} "
        f"(command #{data.get('id')}, {data.get('pending')} pending)."
    )


@mcp.tool()
async def dj_pause() -> str:
    """Pause playback on the Audiplex device."""
    data = await _enqueue("pause", {})
    if isinstance(data, str):
        return data
    return f"Queued pause (command #{data.get('id')}, {data.get('pending')} pending)."


@mcp.tool()
async def dj_resume() -> str:
    """Resume playback on the Audiplex device."""
    data = await _enqueue("resume", {})
    if isinstance(data, str):
        return data
    return f"Queued resume (command #{data.get('id')}, {data.get('pending')} pending)."


@mcp.tool()
async def dj_previous() -> str:
    """Go back to the previous track in the Audiplex device's current queue."""
    data = await _enqueue("previous", {})
    if isinstance(data, str):
        return data
    return f"Queued previous (command #{data.get('id')}, {data.get('pending')} pending)."


@mcp.tool()
async def dj_seek(position_seconds: int) -> str:
    """Seek to an absolute position (in seconds) in the current track."""
    data = await _enqueue("seek", {"position_ms": position_seconds * 1000})
    if isinstance(data, str):
        return data
    return (
        f"Queued seek to {position_seconds}s "
        f"(command #{data.get('id')}, {data.get('pending')} pending)."
    )


@mcp.tool()
async def dj_volume(level: int) -> str:
    """Set the Audiplex app's player volume, 0-100. This is Media3 player
    volume, which multiplies with the phone's device volume — it does NOT
    change the device/stream volume."""
    if not 0 <= level <= 100:
        return f"level must be 0-100 (got {level})."
    data = await _enqueue("volume", {"volume": level / 100.0})
    if isinstance(data, str):
        return data
    return (
        f"Queued volume {level}% "
        f"(command #{data.get('id')}, {data.get('pending')} pending)."
    )


@mcp.tool()
async def dj_play_stream(url: str, title: str = "Live stream") -> str:
    """Play an external HTTP audio stream on the Audiplex device, replacing
    the current queue — this is how agents route Radio Free Luna (or any
    other HTTP audio stream) to the phone, e.g.
    url='http://<rfl-host>:8080/stream.mp3'. Use dj_play_now afterward to
    switch back to music. Play/stop/switch-source only — the queue-ops
    tools (dj_queue/dj_reorder/etc.) don't apply to a stream item.
    """
    data = await _enqueue("play_stream", {"url": url, "title": title})
    if isinstance(data, str):
        return data
    return (
        f"Playing stream '{title}' from {url} "
        f"(command #{data.get('id')}, {data.get('pending')} pending)."
    )


@mcp.tool()
async def dj_break_brief() -> str:
    """Get everything you need to WRITE a DJ voice break: the current daypart's
    persona directive, the local time, optional weather, and what's playing.

    Read-only — this queues nothing. Use it, write 2-4 sentences of on-air copy
    in the register it describes, then pass that copy to dj_announce. Roughly
    one break per 3-5 songs.
    """
    now = datetime.datetime.now()
    part = dj_persona.time_of_day(now)
    persona = dj_persona.DAYPART_PERSONAS[part]

    lines = [
        f"You are {dj_persona.persona_name()}, on air.",
        f"Daypart: {part} ({persona['name']}) — local time "
        f"{now.strftime('%I:%M %p').lstrip('0')}, {now.strftime('%A')}.",
        "",
        f"Directive: {persona['directive']}",
        "",
        f"Style: {dj_persona.STYLE_RULES}",
    ]

    weather = await dj_persona.weather_line()
    if weather:
        lines += ["", f"Outside right now: {weather}."]

    try:
        state = await _get("/api/playback/state")
    except (PermissionError, httpx.HTTPError):
        state = None
    if state and state.get("track"):
        t = state["track"]
        lines += ["", f"Now playing: {t.get('title')} - {t.get('artist')}"]
        upcoming = [
            f"{i.get('title')} - {i.get('artist')}"
            for i in (state.get("queue") or [])
            if i.get("index", -1) > state.get("queue_index", 0)
        ][:3]
        if upcoming:
            lines.append("Coming up: " + "; ".join(upcoming))
    else:
        lines += ["", "Nothing is playing right now."]

    if not tts_backend.is_configured():
        lines += [
            "",
            "WARNING: no TTS backend is configured, so dj_announce will fail. "
            "Set DJ_TTS_URL to an OpenAI-compatible speech endpoint.",
        ]
    return "\n".join(lines)


@mcp.tool()
async def dj_announce(text: str, mode: str = "next", title: str = "DJ break") -> str:
    """Speak a DJ voice break on the device: synthesizes YOUR copy to audio,
    uploads it, and drops it into the queue.

    text:  the on-air copy to speak. Write it yourself with dj_break_brief
           first — every character is synthesized, so no markdown, emoji, or
           bracketed stage directions.
    mode:  'next' (default — plays after the current song finishes, which is
           how a real DJ break lands) or 'now' (interrupt and speak
           immediately).
    title: label shown in the queue (default "DJ break").
    """
    if mode not in ("next", "now"):
        return f"Unknown mode '{mode}'. Use 'next' or 'now'."
    text = (text or "").strip()
    if not text:
        return "No text given; nothing to announce."

    try:
        clip_path = await tts_backend.synthesize(text)
    except tts_backend.TtsNotConfigured as e:
        return f"TTS is not configured: {e}"
    except tts_backend.TtsFailed as e:
        return f"Speech synthesis failed: {e}"

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            with open(clip_path, "rb") as fh:
                resp = await client.post(
                    f"{AUDIPLEX_URL}/api/dj/clips",
                    headers=_headers(),
                    files={"file": (clip_path.name, fh, "application/octet-stream")},
                    data={"title": title},
                )
        if resp.status_code == 401:
            return "Auth failed (401) uploading the clip. Check AUDIPLEX_TOKEN."
        if resp.status_code >= 400:
            return f"Clip upload failed ({resp.status_code}): {resp.text[:300]}"
        clip = resp.json()
    except httpx.HTTPError as e:
        return f"Clip upload failed: {e}"
    finally:
        clip_path.unlink(missing_ok=True)

    data = await _enqueue(
        "announce",
        {
            "clip_id": clip["clip_id"],
            "clip_url": clip["url"],
            "title": title,
            "duration_seconds": clip.get("duration_seconds"),
            "mode": mode,
        },
    )
    if isinstance(data, str):
        return data
    secs = clip.get("duration_seconds")
    length = f"{secs:.1f}s" if isinstance(secs, (int, float)) else "unknown length"
    when = "after the current track" if mode == "next" else "immediately"
    return (
        f"Queued a {length} voice break to play {when} "
        f"(clip #{clip['clip_id']}, command #{data.get('id')}, "
        f"{data.get('pending')} pending)."
    )


async def _get(path: str):
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(f"{AUDIPLEX_URL}{path}", headers=_headers())
    if resp.status_code == 401:
        raise PermissionError("Auth failed (401). Check AUDIPLEX_TOKEN.")
    resp.raise_for_status()
    return resp.json()


def _best_match(items: list[dict], name_key: str, q: str) -> dict | None:
    """Pick the best name match: exact > startswith > substring (all
    case-insensitive). Returns None if nothing contains the query."""
    ql = q.strip().lower()
    exact = [x for x in items if (x.get(name_key) or "").lower() == ql]
    if exact:
        return exact[0]
    starts = [x for x in items if (x.get(name_key) or "").lower().startswith(ql)]
    if starts:
        return starts[0]
    contains = [x for x in items if ql in (x.get(name_key) or "").lower()]
    return contains[0] if contains else None


_MODE_CMD = {"now": "play_now", "queue": "queue", "next": "play_next"}


@mcp.tool()
async def dj_queue_by(
    query: str,
    kind: str = "artist",
    mode: str = "queue",
    limit: int = 100,
) -> str:
    """Resolve a NAME to tracks and play/queue them — no need to look up IDs.

    query: the name to match (case-insensitive: exact > prefix > substring).
           Ignored for kind='favorites' (there's exactly one favorites list).
    kind:  'artist' | 'album' | 'genre' | 'folder' | 'playlist' | 'favorites'.
           For 'folder', query is a folder PATH as returned by dj_library —
           this is the one that works on an untagged library, where the
           artist/genre axes are empty. playlist/favorites resolve against the
           configured DJ owner's library (dj_owner_username), not the caller's
           own — the dj-agent service account has none of its own.
    mode:  'now' (replace current & play), 'queue' (append to end, default),
           'next' (insert after the current track).
    limit: max tracks to enqueue (default 100).

    Resolution is done here in the MCP server over the catalog REST API
    (there is no dedicated /search endpoint). Reports which entity it matched.
    """
    cmd_type = _MODE_CMD.get(mode)
    if cmd_type is None:
        return f"Unknown mode '{mode}'. Use 'now', 'queue', or 'next'."
    try:
        if kind == "artist":
            artists = await _get("/api/music/artists")
            m = _best_match(artists, "name", query)
            if not m:
                return f"No artist matching '{query}'."
            label = f"artist '{m['name']}'"
            tracks = await _get(f"/api/music/artists/{m['id']}/tracks")
        elif kind == "album":
            albums = await _get("/api/music/albums")
            m = _best_match(albums, "title", query)
            if not m:
                return f"No album matching '{query}'."
            artist = m.get("artist_name")
            label = f"album '{m['title']}'" + (f" by {artist}" if artist else "")
            detail = await _get(f"/api/music/albums/{m['id']}")
            tracks = detail.get("tracks", [])
        elif kind == "genre":
            genres = await _get("/api/music/genres")
            m = _best_match(genres, "name", query)
            if not m:
                return f"No genre matching '{query}'."
            label = f"genre '{m['name']}'"
            tracks = await _get(f"/api/music/genres/{quote(m['name'], safe='')}/tracks")
        elif kind == "playlist":
            playlists = await _get("/api/playback/playlists")
            m = _best_match(playlists, "name", query)
            if not m:
                return f"No playlist matching '{query}'."
            label = f"playlist '{m['name']}'"
            detail = await _get(f"/api/playback/playlists/{m['id']}")
            tracks = detail.get("tracks", [])
        elif kind == "folder":
            tracks = await _get(f"/api/music/folders/tracks?path={quote(query, safe='')}")
            label = f"folder '{query}'"
        elif kind == "favorites":
            favorites = await _get("/api/playback/favorites?entity_type=track")
            label = "favorite tracks"
            track_ids_str = [f["entity_key"] for f in favorites]
            tracks = [{"id": int(tid)} for tid in track_ids_str if tid.isdigit()]
        else:
            return (
                f"Unknown kind '{kind}'. Use 'artist', 'album', 'genre', "
                "'folder', 'playlist', or 'favorites'."
            )
    except PermissionError as e:
        return str(e)

    track_ids = [t["id"] for t in tracks][: max(0, limit)]
    if not track_ids:
        return f"Matched {label} but it has no tracks."
    data = await _enqueue(cmd_type, {"track_ids": track_ids})
    if isinstance(data, str):
        return data
    verb = {"now": "Playing", "queue": "Queued", "next": "Playing next"}[mode]
    return (
        f"{verb} {len(track_ids)} track(s) from {label} "
        f"(command #{data.get('id')}, {data.get('pending')} pending)."
    )


@mcp.tool()
async def dj_now_playing() -> str:
    """Report what the Audiplex device is currently playing — track, artist,
    play/pause state, position — plus the full current queue (with indices,
    for dj_reorder), as last reported by the client."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{AUDIPLEX_URL}/api/playback/state", headers=_headers()
        )
    if resp.status_code == 401:
        return "Auth failed (401). Check AUDIPLEX_TOKEN."
    resp.raise_for_status()
    s = resp.json()
    track = s.get("track")
    if not track:
        return "Nothing is playing (no now-playing state reported yet)."
    state = "playing" if s.get("playing") else "paused"
    pos = int(s.get("position_ms", 0) // 1000)
    dur = int(s.get("duration_ms", 0) // 1000)
    idx = s.get("queue_index", 0)
    lines = [
        f"{state}: {track.get('title')} - {track.get('artist')} "
        f"[{pos // 60}:{pos % 60:02d}/{dur // 60}:{dur % 60:02d}] "
        f"(queue {idx + 1}/{s.get('queue_length', 0)})"
    ]
    if s.get("volume") is not None:
        lines.append(f"volume: {round(s['volume'] * 100)}%")
    queue = s.get("queue") or []
    if queue:
        lines.append("Queue:")
        for item in queue:
            marker = "> " if item.get("index") == idx else "  "
            lines.append(
                f"{marker}{item.get('index')}: {item.get('title')} - {item.get('artist')}"
            )
    return "\n".join(lines)


# --- Catalog browsing (item #2943) -----------------------------------------
#
# Why these are title-first rather than artist/album-first: the live library is
# a flat yt-dlp dump. All 206 tracks scanned into ONE album ("music", q:/music)
# under one artist whose name is the EMPTY STRING, with no genres at all — the
# files carry no embedded tags. So /artists, /albums and /genres are degenerate
# and a DJ browsing them alone learns nothing. The real metadata lives in the
# track TITLE strings, which is why dj_tracks + dj_search carry the weight and
# dj_library's job is partly to say "this axis is empty because the files are
# untagged" instead of letting the agent conclude the library is empty.

LONGFORM_SECONDS = 15 * 60

# yt-dlp leaves the encoding tag and the "official video" family in filenames.
# Stripped for DISPLAY ONLY — track IDs and the server's stored titles are
# untouched, so anything shown here can be passed straight back as an ID.
_CRUFT = re.compile(
    r"""\s*(?:
        \(\s*\d+\s*kbit_[A-Za-z0-9]+\s*\)      # (128kbit_AAC), (152kbit_Opus)
      | [\(\[]\s*(?:official\s+)?
          (?:music\s+video|lyric\s+video|lyrics?\s+video|video|audio|
             visuali[sz]er|hd\s+video)
        \s*[\)\]]
      | [\(\[]\s*official\s*[\)\]]
    )""",
    re.IGNORECASE | re.VERBOSE,
)


def _clean_title(title: str) -> str:
    """Strip yt-dlp noise for readability. Cosmetic only."""
    cleaned = _CRUFT.sub("", title or "")
    return re.sub(r"\s{2,}", " ", cleaned).strip(" -–—") or (title or "")


def _fmt_duration(seconds) -> str:
    if not isinstance(seconds, (int, float)) or seconds <= 0:
        return "?:??"
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _is_longform(track: dict) -> bool:
    dur = track.get("duration_seconds")
    return isinstance(dur, (int, float)) and dur >= LONGFORM_SECONDS


def _track_line(track: dict) -> str:
    """`id | title | m:ss` (+ artist when known, + LONG-FORM warning)."""
    artist = (track.get("artist_name") or "").strip()
    title = _clean_title(track.get("title") or "(untitled)")
    label = f"{artist} - {title}" if artist else title
    flag = "  [LONG-FORM — not a song, do not put in a music set]" if _is_longform(track) else ""
    return f"{track.get('id')} | {label} | {_fmt_duration(track.get('duration_seconds'))}{flag}"


async def _all_music_tracks() -> list[dict]:
    """Every track under every music root, de-duplicated by id."""
    listing = await _get("/api/music/folders")
    seen: dict[int, dict] = {}
    for folder in listing.get("folders") or []:
        path = folder.get("path")
        if not path:
            continue
        for t in await _get(f"/api/music/folders/tracks?path={quote(path, safe='')}"):
            seen[t["id"]] = t
    return list(seen.values())


@mcp.tool()
async def dj_library(kind: str = "overview", path: str | None = None) -> str:
    """Survey what's actually IN the library, so you can pick music yourself
    instead of waiting to be told what to play. Read-only; queues nothing.

    kind: 'overview'  (default) — one-shot orientation: music folders with
                       track counts, plus how much each browse axis is worth.
          'folders'   — browse the folder tree; pass `path` to descend.
          'artists' | 'albums' | 'genres' | 'playlists' — list that axis.

    START HERE, then use dj_search / dj_tracks to get the track IDs you feed to
    dj_play_now / dj_queue / dj_play_next.
    """
    try:
        if kind == "overview":
            roots, artists, albums, genres = (
                await _get("/api/music/roots"),
                await _get("/api/music/artists"),
                await _get("/api/music/albums"),
                await _get("/api/music/genres"),
            )
            try:
                playlists = await _get("/api/playback/playlists")
            except httpx.HTTPError:
                playlists = []
            listing = await _get("/api/music/folders")
            folders = listing.get("folders") or []
            total = sum(f.get("track_count", 0) for f in folders)

            lines = [f"MUSIC LIBRARY — {total} track(s) across {len(folders)} folder(s)."]
            for f in folders:
                lines.append(
                    f"  {f.get('name')}  ({f.get('track_count')} tracks, "
                    f"{f.get('album_count')} album(s))  path={f.get('path')}"
                )
            missing = [r["path"] for r in roots.get("roots", []) if not r.get("exists")]
            if missing:
                lines.append(f"  (configured but not on disk right now: {', '.join(missing)})")

            named_artists = [a for a in artists if (a.get("name") or "").strip()]
            tagged_albums = [a for a in albums if (a.get("artist_name") or "").strip()]
            lines += [
                "",
                "Browse axes:",
                f"  artists:   {len(artists)} ({len(named_artists)} actually named)",
                f"  albums:    {len(albums)} ({len(tagged_albums)} with an artist tag)",
                f"  genres:    {len(genres)}",
                f"  playlists: {len(playlists)}"
                + (
                    "  -> " + ", ".join(
                        f"{p.get('name')} ({p.get('track_count', 0)} tracks)" for p in playlists
                    )
                    if playlists
                    else ""
                ),
            ]
            if len(named_artists) < len(artists) or not genres or not tagged_albums:
                lines += [
                    "",
                    "NOTE: the artist/album/genre axes are mostly EMPTY because these files "
                    "carry no embedded tags (a flat yt-dlp dump) — NOT because the library is "
                    "empty. There are real tracks here; the artist and song names live in the "
                    "TRACK TITLES. Use dj_search('<artist or song>') or dj_tracks(folder=...) "
                    "to see them, and don't rely on dj_queue_by(kind='artist'/'genre').",
                ]
            if not playlists or all(p.get("track_count", 0) == 0 for p in playlists):
                lines.append(
                    "NOTE: no non-empty playlists and no favorites exist yet, so "
                    "dj_queue_by(kind='playlist'/'favorites') has nothing to resolve."
                )
            lines += ["", "Next: dj_tracks(folder=...) to page the list, or dj_search('...') to find something."]
            return "\n".join(lines)

        if kind == "folders":
            listing = await _get(
                "/api/music/folders" + (f"?path={quote(path, safe='')}" if path else "")
            )
            lines = [f"Folder: {listing.get('path') or '(music roots)'}"]
            if listing.get("parent"):
                lines.append(f"Parent: {listing['parent']}")
            for f in listing.get("folders") or []:
                lines.append(
                    f"  [dir] {f.get('name')}  ({f.get('track_count')} tracks)  "
                    f"path={f.get('path')}"
                )
            for a in listing.get("albums") or []:
                lines.append(
                    f"  [album] {a.get('title')}  ({a.get('track_count')} tracks)  "
                    f"id={a.get('id')}"
                )
            if len(lines) == 1:
                lines.append("  (nothing here)")
            lines.append("Use dj_tracks(folder='<path>') to list the tracks.")
            return "\n".join(lines)

        if kind == "artists":
            artists = await _get("/api/music/artists")
            if not artists:
                return "No artists. Try dj_tracks/dj_search — the files may be untagged."
            lines = [f"{len(artists)} artist(s):"]
            for a in artists:
                name = (a.get("name") or "").strip()
                lines.append(
                    f"  {a.get('id')} | {name}" if name
                    else f"  {a.get('id')} | (NO NAME — untagged files; use dj_search instead)"
                )
            return "\n".join(lines)

        if kind == "albums":
            albums = await _get("/api/music/albums")
            if not albums:
                return "No albums in the library."
            lines = [f"{len(albums)} album(s):"]
            for a in albums:
                artist = (a.get("artist_name") or "").strip() or "unknown artist"
                lines.append(
                    f"  {a.get('id')} | {a.get('title')} — {artist} "
                    f"({a.get('track_count')} tracks)"
                )
            return "\n".join(lines)

        if kind == "genres":
            genres = await _get("/api/music/genres")
            if not genres:
                return (
                    "No genres — these files carry no genre tags. This does NOT mean the "
                    "library is empty; use dj_library() or dj_search() instead."
                )
            return f"{len(genres)} genre(s):\n" + "\n".join(
                f"  {g.get('name')} ({g.get('track_count', '?')} tracks)" for g in genres
            )

        if kind == "playlists":
            playlists = await _get("/api/playback/playlists")
            if not playlists:
                return "No playlists in the owner's library."
            return f"{len(playlists)} playlist(s):\n" + "\n".join(
                f"  {p.get('id')} | {p.get('name')} ({p.get('track_count', 0)} tracks)"
                for p in playlists
            )

        return (
            f"Unknown kind '{kind}'. Use 'overview', 'folders', 'artists', "
            "'albums', 'genres', or 'playlists'."
        )
    except PermissionError as e:
        return str(e)


@mcp.tool()
async def dj_tracks(
    folder: str | None = None,
    album: str | None = None,
    artist: str | None = None,
    playlist: str | None = None,
    offset: int = 0,
    limit: int = 50,
) -> str:
    """List actual tracks with their IDs, so you can choose what to play.

    Give exactly one of folder (path from dj_library), album, artist or
    playlist (names, matched loosely) — or none, to page the whole library.
    Paged via offset/limit; the footer tells you how to get the next page.

    Each line is `id | title | length`. Long items (podcasts, hours-long focus
    loops) are flagged LONG-FORM — never drop those into a music set.
    """
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    try:
        if folder:
            tracks = await _get(f"/api/music/folders/tracks?path={quote(folder, safe='')}")
            label = f"folder '{folder}'"
        elif album:
            albums = await _get("/api/music/albums")
            m = _best_match(albums, "title", album)
            if not m:
                return f"No album matching '{album}'."
            tracks = (await _get(f"/api/music/albums/{m['id']}")).get("tracks", [])
            label = f"album '{m['title']}'"
        elif artist:
            artists = await _get("/api/music/artists")
            m = _best_match(artists, "name", artist)
            if not m:
                return f"No artist matching '{artist}'. These files are largely untagged — try dj_search('{artist}')."
            tracks = await _get(f"/api/music/artists/{m['id']}/tracks")
            label = f"artist '{m['name'] or '(unnamed)'}'"
        elif playlist:
            playlists = await _get("/api/playback/playlists")
            m = _best_match(playlists, "name", playlist)
            if not m:
                return f"No playlist matching '{playlist}'."
            tracks = (await _get(f"/api/playback/playlists/{m['id']}")).get("tracks", [])
            label = f"playlist '{m['name']}'"
        else:
            tracks = await _all_music_tracks()
            label = "the whole music library"
    except PermissionError as e:
        return str(e)

    if not tracks:
        return f"{label} has no tracks."

    page = tracks[offset : offset + limit]
    if not page:
        return f"{label} has {len(tracks)} track(s); offset {offset} is past the end."

    shown_end = offset + len(page)
    lines = [f"{label} — {len(tracks)} track(s), showing {offset + 1}-{shown_end}:"]
    lines += [f"  {_track_line(t)}" for t in page]
    long_here = sum(1 for t in page if _is_longform(t))
    if long_here:
        lines.append(f"({long_here} flagged LONG-FORM above — keep them out of music sets.)")
    if shown_end < len(tracks):
        lines.append(f"More: repeat with offset={shown_end}.")
    lines.append("Pass any of these IDs to dj_play_now / dj_queue / dj_play_next.")
    return "\n".join(lines)


@mcp.tool()
async def dj_search(query: str, limit: int = 30, include_longform: bool = False) -> str:
    """Find tracks by name — the fastest way to turn an idea into track IDs.

    Matches every whitespace-separated term in `query` against the track title
    and artist (case-insensitive, in any order), so 'ashnikko daisy' works.
    Exact and prefix matches sort first.

    include_longform: False by default, which hides hours-long podcasts and
    focus loops so a music search returns music. Set True to find those on
    purpose (it reports how many it hid).
    """
    q = (query or "").strip()
    if not q:
        return "Give a search query — e.g. dj_search('ashnikko')."
    terms = q.lower().split()
    try:
        tracks = await _all_music_tracks()
    except PermissionError as e:
        return str(e)

    def haystack(t: dict) -> str:
        return f"{t.get('title') or ''} {t.get('artist_name') or ''}".lower()

    matches = [t for t in tracks if all(term in haystack(t) for term in terms)]
    hidden = 0
    if not include_longform:
        kept = [t for t in matches if not _is_longform(t)]
        hidden = len(matches) - len(kept)
        matches = kept

    if not matches:
        msg = f"No tracks matching '{q}'."
        if hidden:
            msg += f" ({hidden} long-form item(s) matched but were hidden — retry with include_longform=True.)"
        else:
            msg += " Try fewer or different words, or dj_library() to see what's there."
        return msg

    ql = q.lower()
    matches.sort(
        key=lambda t: (
            0 if _clean_title(t.get("title") or "").lower() == ql
            else 1 if haystack(t).strip().startswith(ql)
            else 2,
            (t.get("title") or "").lower(),
        )
    )
    page = matches[: max(1, limit)]
    lines = [f"{len(matches)} match(es) for '{q}'" + (f", showing {len(page)}" if len(page) < len(matches) else "") + ":"]
    lines += [f"  {_track_line(t)}" for t in page]
    if hidden:
        lines.append(f"({hidden} long-form item(s) hidden — include_longform=True to see them.)")
    lines.append("Pass these IDs to dj_play_now / dj_queue / dj_play_next.")
    return "\n".join(lines)


# --- Discovery + taste loop (item #2945, phase A) ---------------------------
#
# Lets the DJ propose music that ISN'T in the library yet and learn from how
# those proposals land. Three reasons this is MCP-side SQLite rather than a
# server table: a recommendation has no track row to hang off (play_stats.
# track_id is a FK to tracks, and the whole point is that the track isn't
# there yet); Favorite is binary with no room for a verdict or set context;
# and keeping it out of audiplex.db means no migration and no :8100 restart
# while Todd is listening.
#
# Feedback arrives VOICE-RELAYED through the agent — Todd says "yeah, that one
# was good" out loud and the agent calls dj_rate. There is deliberately no app
# UI for this yet; thumbs in the Android client cost a build, a versionCode
# bump and an in-app update round-trip, which is a lot to spend before we know
# the signal is worth anything.

TASTE_DB = Path(
    os.environ.get("DJ_TASTE_DB")
    or Path(__file__).resolve().parent.parent / "data" / "dj" / "taste.db"
)

# Feedback is dictated and relayed, so it arrives as whatever Todd actually
# said. Anything not recognised as praise is treated as 'meh' — the failure we
# care about is a lukewarm reaction being logged as a win.
_GOOD_WORDS = {
    "good", "great", "yes", "yeah", "yep", "love", "loved", "like", "liked",
    "nice", "banger", "keep", "more", "up", "1", "true",
}
_MEH_WORDS = {
    "meh", "no", "nope", "nah", "not", "bad", "skip", "pass", "down", "0",
    "false", "not-as-good", "notasgood", "worse",
}


@contextlib.contextmanager
def _taste_db():
    """Open (creating on first use) the taste store; commit and close on exit.

    Closing matters: this process is long-lived, and sqlite3's own connection
    context manager commits the transaction but leaves the handle open.
    """
    TASTE_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(TASTE_DB)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS recs (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at   TEXT NOT NULL,
            title        TEXT NOT NULL,
            artist       TEXT NOT NULL DEFAULT '',
            why          TEXT NOT NULL DEFAULT '',
            set_context  TEXT NOT NULL DEFAULT '',
            now_playing  TEXT NOT NULL DEFAULT '',
            verdict      TEXT,
            note         TEXT NOT NULL DEFAULT '',
            rated_at     TEXT
        )"""
        )
        yield conn
        conn.commit()
    finally:
        conn.close()


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _normalize_verdict(verdict: str) -> str:
    """'good' if it reads as praise, else 'meh'. Never guesses in favour of good."""
    words = re.findall(r"[a-z0-9]+", (verdict or "").lower())
    if any(w in _MEH_WORDS for w in words):
        return "meh"
    return "good" if any(w in _GOOD_WORDS for w in words) else "meh"


def _rec_label(row: sqlite3.Row) -> str:
    artist = (row["artist"] or "").strip()
    return f"{artist} - {row['title']}" if artist else row["title"]


async def _current_context() -> str:
    """What's playing right now, best-effort — the context a rec was made in."""
    try:
        state = await _get("/api/playback/state")
    except Exception:
        return ""
    track = state.get("track") or {}
    if not track.get("title"):
        return ""
    artist = (track.get("artist") or "").strip()
    title = _clean_title(track.get("title"))
    return f"{artist} - {title}" if artist else title


@mcp.tool()
async def dj_recommend(
    title: str,
    artist: str = "",
    why: str = "",
    set_context: str = "",
) -> str:
    """Propose a track that is NOT in the library yet, and log the proposal.

    Use this when you want to suggest something new — a track that would fit
    the set but that dj_search can't find because Audiplex doesn't have it.
    Logging it is what makes the suggestion learnable: dj_rate records how it
    landed and dj_taste feeds that back into your later picks.

    This QUEUES NOTHING and DOWNLOADS NOTHING. It records the idea and returns
    a short rec id to quote out loud ("that's rec 7") so Todd's reaction can be
    tied back to it.

    why: one line on why it fits — the reasoning is the part worth learning
         from, so say "same era as what's playing", not "good song".
    set_context: what you're going for right now (e.g. "late-night wind-down").
         What's actually playing is captured automatically.
    """
    name = (title or "").strip()
    if not name:
        return "Give a track title to recommend."
    playing = await _current_context()
    with _taste_db() as conn:
        cur = conn.execute(
            "INSERT INTO recs (created_at, title, artist, why, set_context, now_playing)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (_now(), name, (artist or "").strip(), (why or "").strip(),
             (set_context or "").strip(), playing),
        )
        rec_id = cur.lastrowid
    label = f"{artist.strip()} - {name}" if artist.strip() else name
    lines = [f"Logged rec {rec_id}: {label}"]
    if playing:
        lines.append(f"  (proposed over: {playing})")
    lines.append(
        f'Say the id out loud so it can be rated — then dj_rate({rec_id}, "good"|"meh"). '
        "Nothing was queued or downloaded."
    )
    return "\n".join(lines)


@mcp.tool()
async def dj_rate(rec_id: int = 0, verdict: str = "", note: str = "") -> str:
    """Record how a recommendation landed. This is the human half of the loop.

    rec_id: the id from dj_recommend. Leave it out (or pass 0) to rate the most
        recent UNRATED rec — which is the normal case, because Todd reacts to
        the thing you just suggested ("yeah, that one was good") without
        quoting a number.
    verdict: 'good' or 'meh'. Relay what he actually said; common phrasings are
        understood. Anything not clearly positive is recorded as 'meh' — a
        lukewarm reaction must not be banked as a win.
    note: his own words, if he gave a reason. This is the most useful column in
        the table — "too slow for a workout" teaches more than a bare 'meh'.
    """
    with _taste_db() as conn:
        if rec_id:
            row = conn.execute("SELECT * FROM recs WHERE id = ?", (rec_id,)).fetchone()
            if not row:
                return f"No rec {rec_id}. dj_taste() lists the recent ones."
        else:
            row = conn.execute(
                "SELECT * FROM recs WHERE verdict IS NULL ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if not row:
                return (
                    "No unrated recommendation to rate. Pass an explicit rec_id, "
                    "or dj_taste() to see what's been logged."
                )
        v = _normalize_verdict(verdict)
        # Keep the old note when this call doesn't carry one: correcting a
        # verdict must not wipe the reason he gave the first time, which is the
        # highest-signal thing in the table.
        new_note = (note or "").strip() or row["note"]
        conn.execute(
            "UPDATE recs SET verdict = ?, note = ?, rated_at = ? WHERE id = ?",
            (v, new_note, _now(), row["id"]),
        )
    was = f" (was already rated '{row['verdict']}')" if row["verdict"] else ""
    tail = f' — "{note.strip()}"' if (note or "").strip() else ""
    return (
        f"Rec {row['id']} ({_rec_label(row)}) rated {v}{tail}.{was}\n"
        "dj_taste() folds this into your next picks."
    )


@mcp.tool()
async def dj_taste(limit: int = 20) -> str:
    """Read back what's been learned about Todd's taste — check this BEFORE
    recommending, so picks improve instead of repeating.

    Three things: the verdicts on your past recommendations (with his own
    words, which carry the most signal), recs still awaiting a reaction, and
    the library-side play history.
    """
    limit = max(1, min(limit, 200))
    with _taste_db() as conn:
        rated = conn.execute(
            "SELECT * FROM recs WHERE verdict IS NOT NULL ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        unrated = conn.execute(
            "SELECT * FROM recs WHERE verdict IS NULL ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        totals = dict(
            conn.execute(
                "SELECT verdict, COUNT(*) FROM recs WHERE verdict IS NOT NULL"
                " GROUP BY verdict"
            ).fetchall()
        )

    lines: list[str] = []
    if not rated and not unrated:
        lines.append(
            "No recommendations logged yet — nothing learned. Use dj_recommend() "
            "when you suggest something that isn't in the library, then dj_rate() "
            "when Todd reacts."
        )
    else:
        lines.append(
            f"Rated recs: {totals.get('good', 0)} good / {totals.get('meh', 0)} meh."
        )
    for row in rated:
        bits = [f"  [{row['verdict']}] {row['id']}: {_rec_label(row)}"]
        if row["note"]:
            bits.append(f'      he said: "{row["note"]}"')
        if row["why"]:
            bits.append(f"      your reasoning: {row['why']}")
        ctx = row["set_context"] or row["now_playing"]
        if ctx:
            bits.append(f"      context: {ctx}")
        lines += bits
    if unrated:
        lines.append(f"Awaiting a reaction ({len(unrated)}) — ask about these:")
        lines += [f"  {row['id']}: {_rec_label(row)}" for row in unrated]

    # Library-side signal. Both endpoints are scoped to the CALLING user, and
    # the DJ calls as dj-agent, which has never played anything — so an empty
    # result here means "not visible to me", NOT "Todd doesn't listen to music".
    # Saying so is the whole point; a silent [] would read as the opposite.
    try:
        played = await _get("/api/music/most-played?limit=10")
        skipped = await _get("/api/music/likely-skips?limit=10")
    except Exception as e:
        lines.append(f"(Library play history unavailable: {e})")
        return "\n".join(lines)

    if played:
        lines.append("Most-played in the library:")
        lines += [f"  {_track_line(t)}" for t in played]
    if skipped:
        lines.append("Often skipped early — avoid these:")
        lines += [
            f"  {_track_line(t.get('track', t))}"
            f"  ({t.get('early_skip_count')} early skips of {t.get('total_starts')} starts)"
            for t in skipped
        ]
    if not played and not skipped:
        lines.append(
            "No library play history visible: /api/music/most-played and "
            "/likely-skips are scoped to the CALLING user and the DJ calls as "
            "dj-agent, which has no listening history of its own. Todd's plays "
            "are recorded under his own account and are not readable here — "
            "treat this as no data, not as dislike."
        )
    return "\n".join(lines)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
