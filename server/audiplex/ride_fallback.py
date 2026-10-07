"""Ride-start fallback (#3924): music starts even when the DJ persona's lookups stall.

On Todd's 2026-10-06 ride the persona's dj_pool_set and dj_search hung for more
than 120 s and he rode about five minutes in silence. Pantheon's bike-mode sweep
calls POST /api/playback/ride-fallback when a ride started (automatic bike
detection) but nothing is playing ~90 s later. It plays the announce clip now
and, START_GAP_S later, starts the pool on the ride spec's SOURCES (#7190: all
of them, folders, folder matches and buckets, not just the bucket objects): one
lane per source, so the pool's even round-robin gives each a turn, picks within
a lane are shuffled (#7108), and recent plays are skipped. Never the phone's
last queue, which can be an audiobook or the sleep queue.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from sqlalchemy import text

logger = logging.getLogger(__name__)

RIDE_SPEC = "todd-ride-mix"
START_GAP_S = 10.0  # announce first; music follows about this long after
POOL_AHEAD = 300  # same deep queue the MCP's dj_pool_set asks for (#3644)
POOL_REFILL_AT = 20

last: dict[str, Any] = {}  # the latest outcome, for GET /ride-fallback
_tasks: set[asyncio.Task] = set()


def _buckets():
    try:
        from audiplex_mcp import buckets
    except ImportError:  # the server runs from server/, like dj_triggers.render_say
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from audiplex_mcp import buckets
    return buckets


RESOLVE_BUDGET_S = 5.0  # #7190: a source still unresolved by then is skipped, never waited on


def _source_ids(db, src: dict, music_roots: list[str]) -> list[int]:
    """One spec source -> track ids, in-process (no HTTP), mirroring the MCP's
    _resolve_source for the kinds a ride spec uses. Raises on anything else."""
    from audiplex.models import Album, Track
    from audiplex.routers.music import _norm, get_folder_tracks, match_folders

    kind, query = str(src.get("kind", "folder")), str(src.get("query", ""))
    if kind == "tracks":
        return [int(i) for i in src.get("ids") or []]
    if kind == "bucket":
        b = _buckets().find_bucket(query)
        if not b:
            raise LookupError(f"no bucket matching '{query}'")
        return [int(t["track_id"]) for t in b["tracks"]]
    if kind == "folder" and not src.get("recursive", True):  # loose files only (#5473)
        here = _norm(query)
        album_ids = [aid for aid, fp in db.query(Album.id, Album.folder_path) if _norm(fp) == here]
        rows = db.query(Track.id).filter(Track.album_id.in_(album_ids)).all() if album_ids else []
        return [r[0] for r in rows]
    if kind == "folder":
        return [t.id for t in get_folder_tracks(path=query, db=db, user=None)]
    if kind == "folder_match":
        seen: dict[int, None] = {}
        for path in match_folders(q=query, db=db, user=None, music_roots=music_roots):
            for t in get_folder_tracks(path=path, db=db, user=None):
                seen.setdefault(t.id, None)
        return list(seen)
    raise LookupError(f"source kind '{kind}' is not resolved by the ride fallback")


def ride_lanes(db, spec: str = RIDE_SPEC, music_roots: list[str] | None = None,
               budget_s: float = RESOLVE_BUDGET_S) -> tuple[dict[str, list[int]], list[str]]:
    """({label: track_ids}, skipped) for every source of the ride spec: the same
    lanes dj_pool_set builds (#7190). A source that fails, is empty, or comes up
    after the budget is spent is skipped and named, never waited on."""
    if music_roots is None:
        from audiplex.routers.music import get_music_roots
        music_roots = get_music_roots()
    row = db.execute(
        text("SELECT sources_json FROM dj_mix_specs WHERE name = :name"), {"name": spec}
    ).first()
    sources = json.loads(row[0]) if row and row[0] else []
    deadline = time.monotonic() + budget_s
    lanes: dict[str, list[int]] = {}
    skipped: list[str] = []
    for src in sources:
        label = str(src.get("label") or src.get("query") or src.get("kind"))
        if time.monotonic() > deadline:
            skipped.append(f"{label}: out of time")
            continue
        try:
            ids = _source_ids(db, src, music_roots)
        except Exception as e:  # skip it, don't stall the ride
            skipped.append(f"{label}: {e}")
            continue
        if ids:
            lanes[label] = ids
        else:
            skipped.append(f"{label}: no tracks")
    if skipped:
        logger.warning("ride fallback skipped sources: %s", skipped)
    return lanes, skipped


def schedule(coro: Awaitable) -> None:
    """Run the delayed start in the background (tests replace this)."""
    task = asyncio.ensure_future(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def start_after_announce(
    bus, set_pool: Callable[[], dict], delay_s: float,
    latched: Callable[[], bool], sleep=asyncio.sleep,
) -> dict:
    """After the announce: start the pool and play its first picks, unless music
    already started (a persona caught up) or a stop was latched meanwhile."""
    await sleep(delay_s)
    if (bus.get_state() or {}).get("playing"):
        out = {"started": False, "reason": "music started during the announce"}
    elif latched():
        out = {"started": False, "reason": "a stop is latched"}
    else:
        try:
            picks = (set_pool() or {}).get("initial_picks") or []
        except Exception as e:  # a 409 from set_pool, or a DB error
            picks, err = [], getattr(e, "detail", None) or str(e)
            out = {"started": False, "reason": f"pool not started: {err}"}
        else:
            if picks:
                ids = bus.send_tracks("play_now", picks, source="ride_fallback")
                out = {"started": True, "count": len(picks), "command_ids": ids}
            else:
                out = {"started": False, "reason": "no eligible picks (everything recent)"}
    last.clear()
    last.update({**out, "phase": "music", "at": time.time()})
    logger.warning("ride fallback music: %s", out)
    return out
