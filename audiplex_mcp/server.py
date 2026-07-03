"""Audiplex DJ — dedicated MCP server.

Exposes the Audiplex playback command bus as agent ("DJ") tools. Kept as a
standalone package (NOT folded into pantheon_mcp_server) so other Pantheon
adopters can run it independently against their own Audiplex instance.

Config via environment:
  AUDIPLEX_URL    base URL of the Audiplex server (e.g. http://100.x.y.z:8000)
  AUDIPLEX_TOKEN  service-account JWT — mint via:
                    cd server && python -m audiplex.create_service_token

Tools: dj_play_now, dj_skip, dj_queue, dj_play_next, dj_reorder,
dj_queue_by, dj_now_playing. The agent can pass explicit track IDs (resolved
via the catalog REST API) or let dj_queue_by resolve an artist/album/genre
NAME to tracks MCP-side (there is no dedicated /search endpoint).
"""

import os
from urllib.parse import quote

import httpx
from mcp.server.fastmcp import FastMCP

AUDIPLEX_URL = os.environ.get("AUDIPLEX_URL", "http://localhost:8000").rstrip("/")
AUDIPLEX_TOKEN = os.environ.get("AUDIPLEX_TOKEN", "")

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
    kind:  'artist' | 'album' | 'genre'.
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
        else:
            return f"Unknown kind '{kind}'. Use 'artist', 'album', or 'genre'."
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
    queue = s.get("queue") or []
    if queue:
        lines.append("Queue:")
        for item in queue:
            marker = "> " if item.get("index") == idx else "  "
            lines.append(
                f"{marker}{item.get('index')}: {item.get('title')} - {item.get('artist')}"
            )
    return "\n".join(lines)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
