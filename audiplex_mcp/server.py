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

import asyncio
import contextlib
import datetime
import os
import re
import shutil
import sqlite3
import sys
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
        device_resp = await client.get(
            f"{AUDIPLEX_URL}/api/playback/device", headers=_headers()
        )
    data = resp.json()
    device = device_resp.json() if device_resp.status_code == 200 else {}
    head = (
        f"Queued play_now for {len(track_ids)} track(s) "
        f"(command #{data.get('id')}, {data.get('pending')} pending). "
    )
    # Never claim "it's playing" — say whether anything is listening, so a dead
    # player reads as a failure instead of a success (#2961).
    if not device:
        return head + "The device plays when it next polls (immediately if awake)."
    if device.get("connected"):
        return head + "A player is connected, so it should pick this up within seconds."
    return head + _describe_device(device) + " It will play whenever a player next starts."


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


def _describe_age(seconds: float | None) -> str:
    if seconds is None:
        return "never"
    if seconds < 60:
        return f"{int(seconds)}s ago"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    return f"{seconds / 3600:.1f}h ago"


def _describe_device(d: dict) -> str:
    """One line on whether a player is actually out there (#2961)."""
    if not d.get("connected"):
        last = _describe_age(d.get("last_poll_age_seconds"))
        pending = d.get("pending", 0)
        tail = f" {pending} command(s) waiting for it." if pending else ""
        if d.get("last_poll_at") is None:
            return f"NO PLAYER CONNECTED (none has ever polled this server).{tail}"
        return f"NO PLAYER CONNECTED (last poll {last}).{tail}"
    return f"Player connected (last poll {_describe_age(d.get('last_poll_age_seconds'))})."


@mcp.tool()
async def dj_device_status() -> str:
    """Is an Audiplex player actually alive and listening right now?

    Answers the question dj_now_playing cannot: a device that is connected but
    idle and a device that is dead both report nothing playing. Use this before
    concluding a play command failed.
    """
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{AUDIPLEX_URL}/api/playback/device", headers=_headers()
        )
    if resp.status_code == 401:
        return "Auth failed (401). Check AUDIPLEX_TOKEN."
    resp.raise_for_status()
    d = resp.json()
    lines = [_describe_device(d)]
    if d.get("last_command_id"):
        lines.append(
            f"Last command taken: #{d['last_command_id']} ({d.get('last_command_type')}) "
            f"{_describe_age(d.get('last_command_delivered_age_seconds'))}."
        )
    if d.get("ever_reported_state"):
        lines.append(
            f"Last now-playing report: {_describe_age(d.get('last_state_age_seconds'))}."
        )
    else:
        lines.append("Last now-playing report: never (no state has ever been reported).")
    if d.get("pending"):
        lines.append(f"{d['pending']} command(s) still queued.")
    return "\n".join(lines)


@mcp.tool()
async def dj_client_log(limit: int = 25) -> str:
    """Recent diagnostics shipped up by the Android player — playback errors and
    process-exit reasons. This is how you find out WHY audio stopped or the app
    died; the phone is not reachable from the server host any other way."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{AUDIPLEX_URL}/api/playback/client-log",
            headers=_headers(),
            params={"limit": max(1, min(limit, 200))},
        )
    if resp.status_code == 401:
        return "Auth failed (401). Check AUDIPLEX_TOKEN."
    resp.raise_for_status()
    entries = resp.json()
    if not entries:
        return "No client diagnostics reported."
    return _render_client_log(entries)


def _render_client_log(entries: list) -> str:
    """One line per entry, except a stack trace, which gets its own block.

    A trace inlined into the comma-joined detail dict is unreadable — which is
    part of why traces were not being shipped at all before #3021. Pull it out
    and indent it instead.
    """
    lines = []
    for e in entries:
        when = datetime.datetime.fromtimestamp(e.get("received_at", 0)).strftime("%H:%M:%S")
        line = f"[{when}] {e.get('level', 'info').upper()} {e.get('event')}: {e.get('message', '')}"
        detail = dict(e.get("detail") or {})
        trace = detail.pop("trace", "")
        if detail:
            line += " " + ", ".join(f"{k}={v}" for k, v in detail.items())
        lines.append(line.rstrip())
        if trace:
            lines.extend("    " + t for t in str(trace).splitlines())
    return "\n".join(lines)


@mcp.tool()
async def dj_client_exits(limit: int = 25) -> str:
    """Process-exit reports that SURVIVED a server restart.

    dj_client_log reads an in-memory ring buffer, so a restart wipes it — and
    the phone advances its own report watermark the moment the server accepts
    an entry, so it never re-sends one. That makes a death report the one
    diagnostic that can be lost for good, which is why it is also written to
    disk (#3021). Reach for this when dj_client_log looks emptier than it
    should, or when you need history older than the buffer holds."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{AUDIPLEX_URL}/api/playback/client-exits",
            headers=_headers(),
            params={"limit": max(1, min(limit, 200))},
        )
    if resp.status_code == 401:
        return "Auth failed (401). Check AUDIPLEX_TOKEN."
    resp.raise_for_status()
    entries = resp.json()
    if not entries:
        return "No process exits on record."
    return _render_client_log(entries)


@mcp.tool()
async def dj_now_playing() -> str:
    """Report what the Audiplex device is currently playing — track, artist,
    play/pause state, position — plus the full current queue (with indices,
    for dj_reorder), as last reported by the client.

    Also reports device liveness, so 'nothing playing' can be told apart from
    'nothing listening' (#2961)."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{AUDIPLEX_URL}/api/playback/state", headers=_headers()
        )
        if resp.status_code == 401:
            return "Auth failed (401). Check AUDIPLEX_TOKEN."
        resp.raise_for_status()
        device_resp = await client.get(
            f"{AUDIPLEX_URL}/api/playback/device", headers=_headers()
        )
    s = resp.json()
    device = device_resp.json() if device_resp.status_code == 200 else {}
    device_line = _describe_device(device) if device else ""
    track = s.get("track")
    if not track:
        why = (
            "The player is connected and idle — nothing is loaded."
            if device.get("connected")
            else "No player has reported state."
        )
        return f"Nothing is playing. {why}\n{device_line}".rstrip()
    age = device.get("last_state_age_seconds")
    if age is not None and age > 60:
        # Stale state is worse than no state: it reads as live and isn't.
        device_line += (
            f"\nWARNING: this snapshot is {_describe_age(age)} — "
            "the player may have stopped or died since."
        )
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
    if device_line:
        lines.append(device_line)
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
             visuali[sz]er|hd\s+video|lyrics?)
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
        conn.execute(
            """CREATE TABLE IF NOT EXISTS candidates (
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


# --------------------------------------------------------------------------
# Ingest (#2945 phase B) — find a track, get Todd's yes, download it TAGGED.
#
# Two tools on purpose. dj_find_candidates SEARCHES and downloads nothing;
# dj_ingest only accepts a candidate id that search produced, plus what Todd
# actually said when he approved it. So the DJ cannot go from "I like this
# song" to a file on disk without a candidate having been read out loud first,
# and every download carries an audit row saying who approved it and how.
#
# WHERE the file lands is the whole ballgame, and it is not where you'd guess.
# The music scanner is PATH-FIRST (server/audiplex/scanners/music.py): album
# title is the folder name and artist is the folder's PARENT — tags only supply
# per-track title and year. That is exactly why the existing dump is degenerate:
# 206 files sitting loose in q:\music make one album called "music" whose
# artist is the parent of the root, i.e. the empty string. Writing perfect tags
# on a file dropped in beside them would change NOTHING about the browse axes.
# So ingest builds <root>/<Artist>/<Album>/ (or the scanner's genre layout,
# <root>/Artists & Albums/<Genre>/<Artist>/<Album>/, when a genre is given) and
# writes the tags as well, since title/year still come from them.

FFMPEG_FALLBACK = r"C:\ProgramData\chocolatey\bin\ffmpeg.exe"
GENRE_PARENT = "Artists & Albums"

# Downloads land here first and are only moved into the library once they are
# a real, tagged .m4a. A half-written file inside a music root would otherwise
# be visible to the very rescan we fire at the end.
STAGING_DIR = TASTE_DB.parent / "staging"

_ARTIST_TITLE_SEP = re.compile(r"\s+[-–—]\s+")
_ILLEGAL_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _split_artist_title(raw_title: str, uploader: str = "") -> tuple[str, str]:
    """Best-effort "Artist - Title" split off a YouTube title.

    Falls back to the channel name with the " - Topic" suffix that YouTube's
    auto-generated artist channels carry stripped off.
    """
    cleaned = _clean_title(raw_title or "")
    parts = _ARTIST_TITLE_SEP.split(cleaned, maxsplit=1)
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        return parts[0].strip(), parts[1].strip()
    channel = re.sub(r"\s*-\s*Topic\s*$", "", uploader or "", flags=re.IGNORECASE)
    return channel.strip(), cleaned


def _plain(err: Exception) -> str:
    """yt-dlp colours its errors; ANSI escapes are noise in a relayed message."""
    return re.sub(r"\x1b\[[0-9;]*m", "", str(err)).strip()


def _safe_name(name: str, fallback: str = "Unknown") -> str:
    """A path segment Windows will actually accept."""
    cleaned = _ILLEGAL_FILENAME.sub("", name or "").strip().rstrip(". ")
    return cleaned[:120] or fallback


def _match_key(*parts: str) -> str:
    """Order-insensitive word key, for spotting a track we already have."""
    words = re.findall(r"[a-z0-9]+", _clean_title(" ".join(parts)).lower())
    return " ".join(sorted(words))


def _require(module: str, package: str):
    try:
        return __import__(module)
    except ImportError:
        raise RuntimeError(
            f"{module} is not installed for this interpreter "
            f"({sys.executable}). Install it with: pip install --user {package}"
        ) from None


def _search_youtube(query: str, limit: int) -> list[dict]:
    """Metadata-only search. Downloads nothing (`extract_flat` + no download)."""
    _require("yt_dlp", "yt-dlp")
    from yt_dlp import YoutubeDL

    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        # This process speaks JSON-RPC over stdout. yt-dlp's progress output
        # goes there by default and would corrupt the MCP transport mid-set,
        # so force every byte it emits onto stderr.
        "logtostderr": True,
        "noprogress": True,
        "skip_download": True,
        "extract_flat": True,
        "socket_timeout": 20,
    }
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
    return [e for e in (info or {}).get("entries") or [] if e]


def _download_audio(url: str, dest_dir: Path) -> tuple[Path, dict]:
    """Download `url` as audio into an EMPTY `dest_dir`; return (file, info)."""
    _require("yt_dlp", "yt-dlp")
    from yt_dlp import YoutubeDL

    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "logtostderr": True,  # keep yt-dlp off stdout — see _search_youtube
        "noprogress": True,
        "socket_timeout": 30,
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "outtmpl": str(dest_dir / "dl.%(ext)s"),
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "m4a", "preferredquality": "0"}
        ],
    }
    ffmpeg = shutil.which("ffmpeg") or (
        FFMPEG_FALLBACK if Path(FFMPEG_FALLBACK).exists() else None
    )
    if ffmpeg:
        opts["ffmpeg_location"] = ffmpeg

    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True) or {}

    produced = sorted(dest_dir.glob("dl.*"))
    m4a = [p for p in produced if p.suffix.lower() == ".m4a"]
    if not m4a:
        got = ", ".join(p.name for p in produced) or "nothing"
        raise RuntimeError(
            f"Download produced {got}, not an .m4a — ffmpeg conversion likely "
            "failed. Left in staging rather than putting an untaggable file in "
            "the library."
        )
    return m4a[0], info


def _write_tags(path: Path, title: str, artist: str, album: str, year=None) -> None:
    """Write the APPROVED metadata over whatever the source file carried.

    Deliberately not "keep the source tags if present": the existing library
    shows where that leads — Marillion's tracks all claim "Various Artists"
    and Barracuda carries no artist at all.
    """
    _require("mutagen", "mutagen")
    from mutagen.mp4 import MP4

    audio = MP4(str(path))
    audio["\xa9nam"] = [title]
    audio["\xa9ART"] = [artist]
    audio["aART"] = [artist]
    audio["\xa9alb"] = [album]
    if year:
        audio["\xa9day"] = [str(year)]
    audio.save()


async def _music_root() -> Path:
    roots = (await _get("/api/music/roots")).get("roots") or []
    usable = [r for r in roots if r.get("exists") and r.get("path")]
    if not usable:
        raise RuntimeError("No readable music root is configured on the server.")
    return Path(usable[0]["path"])


@mcp.tool()
async def dj_find_candidates(query: str, limit: int = 5, rec_id: int = 0) -> str:
    """Find real, downloadable sources for a track the library does NOT have —
    so a recommendation can become something Todd can actually play.

    THIS DOWNLOADS NOTHING. It searches and returns candidates with a candidate
    id each. Read the best one out to Todd; if he says yes, and only then, call
    dj_ingest with that id. That two-step is the approval gate — dj_ingest will
    not take a bare URL.

    query: what to search for — "artist track name" works best.
    rec_id: the dj_recommend id this came from, if any, so the taste loop and
        the download stay connected.
    """
    q = (query or "").strip()
    if not q:
        return "Give something to search for — 'artist track name'."
    limit = max(1, min(limit, 10))

    try:
        entries = await asyncio.to_thread(_search_youtube, q, limit)
    except Exception as e:
        return f"Search failed: {_plain(e)}"
    if not entries:
        return f"No results for '{q}'."

    try:
        library = await _all_music_tracks()
        have = {_match_key(t.get("title") or "") for t in library}
    except Exception:
        have = set()

    lines = [f"Candidates for '{q}' — nothing downloaded:"]
    with _taste_db() as conn:
        for e in entries:
            url = e.get("url") or e.get("webpage_url") or (
                f"https://www.youtube.com/watch?v={e['id']}" if e.get("id") else ""
            )
            if not url:
                continue
            raw = e.get("title") or "(untitled)"
            artist, title = _split_artist_title(
                raw, e.get("channel") or e.get("uploader") or ""
            )
            dur = e.get("duration")
            cur = conn.execute(
                "INSERT INTO candidates (created_at, query, url, title, artist,"
                " duration, rec_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (_now(), q, url, title, artist, dur, rec_id or None),
            )
            label = f"{artist} - {title}" if artist else title
            flags = []
            if isinstance(dur, (int, float)) and dur >= LONGFORM_SECONDS:
                flags.append("LONG-FORM — almost certainly not the song")
            if _match_key(artist, title) in have or _match_key(title) in have:
                flags.append("library already has something matching this")
            tail = f"  [{'; '.join(flags)}]" if flags else ""
            lines.append(f"  {cur.lastrowid} | {label} | {_fmt_duration(dur)}{tail}")

    lines.append(
        "Say the one you mean out loud and get Todd's yes, then "
        'dj_ingest(<id>, approval="<what he said>").'
    )
    return "\n".join(lines)


@mcp.tool()
async def dj_ingest(
    candidate_id: int,
    approval: str,
    artist: str = "",
    title: str = "",
    album: str = "",
    genre: str = "",
    allow_longform: bool = False,
    allow_duplicate: bool = False,
) -> str:
    """Download an APPROVED candidate into the library, properly tagged, and
    rescan so it's immediately playable.

    ONLY call this after Todd has said yes to a specific candidate from
    dj_find_candidates. `approval` is what he actually said — it is required,
    recorded, and it is the only evidence that a human authorised the download.
    Never fill it in on his behalf.

    artist/title/album/genre: override the search metadata when it's wrong —
        and check it, because YouTube titles are frequently wrong. These become
        the FOLDER LAYOUT as well as the tags, which is what makes the track
        browsable by artist instead of joining the untagged pile: with a genre
        it lands in `Artists & Albums/<Genre>/<Artist>/<Album>/`, without one in
        `<Artist>/<Album>/`. Album defaults to "Singles".
    allow_longform: required to ingest anything 15+ minutes — normally a sign
        the candidate is a mix or a full album upload, not the song.
    allow_duplicate: required if the library already looks to have this track.
    """
    said = (approval or "").strip()
    if not said:
        return (
            "Refusing: `approval` is empty. Ask Todd first and pass what he "
            "said — this tool downloads a file and adds it to his library."
        )
    with _taste_db() as conn:
        row = conn.execute(
            "SELECT * FROM candidates WHERE id = ?", (candidate_id,)
        ).fetchone()
    if not row:
        return (
            f"No candidate {candidate_id}. Run dj_find_candidates first and use "
            "an id it returned — this tool won't take a bare URL."
        )
    if row["ingested_at"]:
        return (
            f"Candidate {candidate_id} was already ingested on {row['ingested_at']}"
            f" → {row['ingested_path']}"
        )

    dur = row["duration"]
    if (
        not allow_longform
        and isinstance(dur, (int, float))
        and dur >= LONGFORM_SECONDS
    ):
        return (
            f"Refusing: that candidate is {_fmt_duration(dur)} — long enough to "
            "be a mix or a full-album upload rather than the track. Check it's "
            "really what you want, then pass allow_longform=True."
        )

    final_artist = (artist or row["artist"] or "").strip()
    final_title = (title or row["title"] or "").strip()
    if not final_title:
        return "Refusing: no title to tag this with. Pass title=..."
    if not final_artist:
        return (
            "Refusing: no artist. The artist is the folder name the scanner "
            "reads, so a blank one lands this in the untagged pile — the exact "
            "thing this is meant to stop. Pass artist=..."
        )
    final_album = (album or "").strip() or "Singles"

    if not allow_duplicate:
        try:
            have = {_match_key(t.get("title") or "") for t in await _all_music_tracks()}
        except Exception:
            have = set()
        if _match_key(final_artist, final_title) in have or _match_key(final_title) in have:
            return (
                f"Refusing: the library already looks to have '{final_artist} - "
                f"{final_title}'. dj_search to check; pass allow_duplicate=True "
                "if it really is a different recording."
            )

    try:
        root = await _music_root()
    except Exception as e:
        return f"Can't resolve the music root: {e}"

    parts = [_safe_name(final_artist), _safe_name(final_album)]
    if genre.strip():
        parts = [GENRE_PARENT, _safe_name(genre.strip())] + parts
    dest_dir = root.joinpath(*parts)
    dest = dest_dir / f"{_safe_name(final_title, 'track')}.m4a"
    if dest.exists():
        return f"Refusing: {dest} already exists. Nothing downloaded."

    staging = STAGING_DIR / f"c{candidate_id}"
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)

    try:
        downloaded, info = await asyncio.to_thread(_download_audio, row["url"], staging)
    except Exception as e:
        # Nothing here is worth keeping — partial fragments only. (Tagging and
        # move failures below deliberately DO keep the file.)
        shutil.rmtree(staging, ignore_errors=True)
        return f"Download failed, nothing added to the library: {_plain(e)}"

    # Duration is only trustworthy now — search gave a flat estimate, and the
    # long-form guard is worth nothing if the real file turns out to be an hour.
    real_dur = info.get("duration")
    if (
        not allow_longform
        and isinstance(real_dur, (int, float))
        and real_dur >= LONGFORM_SECONDS
    ):
        shutil.rmtree(staging, ignore_errors=True)
        return (
            f"Discarded: the downloaded file is {_fmt_duration(real_dur)}, not "
            "the short track the search suggested. Nothing was added. Pass "
            "allow_longform=True if you really want it."
        )

    try:
        await asyncio.to_thread(
            _write_tags, downloaded, final_title, final_artist, final_album,
            info.get("release_year"),
        )
    except Exception as e:
        return (
            f"Tagging failed: {e}. File left in {staging} and NOT added — an "
            "untagged file is what we're trying to stop shipping."
        )

    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(downloaded), str(dest))
    except OSError as e:
        return f"Could not move into the library: {e}. File is still in {staging}."
    shutil.rmtree(staging, ignore_errors=True)

    with _taste_db() as conn:
        conn.execute(
            "UPDATE candidates SET approval = ?, ingested_at = ?, ingested_path = ?"
            " WHERE id = ?",
            (said, _now(), str(dest), candidate_id),
        )

    lines = [
        f"Ingested: {final_artist} - {final_title}"
        f" ({_fmt_duration(real_dur or dur)})",
        f"  {dest}",
        f'  tagged artist/title/album, approved by Todd: "{said}"',
    ]

    # Music-roots-only rescan: this is the endpoint the DJ token is allowed to
    # call (#2947). Without it the file is on disk but invisible to the catalog.
    try:
        async with httpx.AsyncClient(timeout=180) as client:
            resp = await client.post(
                f"{AUDIPLEX_URL}/api/library/scan/music", headers=_headers()
            )
        resp.raise_for_status()
        scan = resp.json()
        lines.append(
            f"  rescan: added={scan.get('added')} updated={scan.get('updated')}"
            f" removed={scan.get('removed')}"
        )
        for err in (scan.get("errors") or [])[:3]:
            lines.append(f"  scan warning: {err}")
    except Exception as e:
        lines.append(
            f"  RESCAN FAILED ({e}) — the file is in place but the catalog "
            "hasn't picked it up, so it isn't playable yet."
        )
        return "\n".join(lines)

    found = []
    try:
        found = [
            t for t in await _all_music_tracks()
            if _match_key(t.get("title") or "") == _match_key(final_title)
        ]
    except Exception:
        pass
    if found:
        lines.append(f"  now playable: {_track_line(found[0])}")
    else:
        lines.append(
            "  NOTE: rescan ran but the track isn't showing in the catalog yet."
        )
    return "\n".join(lines)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
