"""Audiplex DJ — dedicated MCP server.

Exposes the Audiplex playback command bus as agent ("DJ") tools. Kept as a
standalone package (NOT folded into pantheon_mcp_server) so other Pantheon
adopters can run it independently against their own Audiplex instance.

Config via environment:
  AUDIPLEX_URL    base URL of the Audiplex server (e.g. http://100.x.y.z:8000)
  AUDIPLEX_TOKEN  service-account JWT — mint via:
                    cd server && python -m audiplex.create_service_token

v1 tools: dj_play_now, dj_now_playing. The richer toolset
(dj_queue / dj_play_next / dj_skip / dj_search) lands in P3; the agent
resolves vague requests -> track IDs via the existing catalog REST API.
"""

import os

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


@mcp.tool()
async def dj_now_playing() -> str:
    """Report what the Audiplex device is currently playing — track, artist,
    play/pause state, and position — as last reported by the client."""
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
    return (
        f"{state}: {track.get('title')} - {track.get('artist')} "
        f"[{pos // 60}:{pos % 60:02d}/{dur // 60}:{dur % 60:02d}] "
        f"(queue {s.get('queue_index', 0) + 1}/{s.get('queue_length', 0)})"
    )


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
