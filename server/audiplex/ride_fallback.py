"""Ride-start fallback (#3924): music starts even when the DJ persona's lookups stall.

On Todd's 2026-10-06 ride the persona's dj_pool_set and dj_search hung for more
than 120 s and he rode about five minutes in silence. Pantheon's bike-mode sweep
calls POST /api/playback/ride-fallback when a ride started (automatic bike
detection) but nothing is playing ~90 s later. It plays the announce clip now
and, START_GAP_S later, starts the pool on the ride spec's BUCKETS only: one
lane per bucket, so the pool's even round-robin gives each bucket a turn, picks
within a lane are shuffled (#7108), and recent plays are skipped. Never the
phone's last queue, which can be an audiobook or the sleep queue.
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


def ride_bucket_lanes(db, spec: str = RIDE_SPEC) -> dict[str, list[int]]:
    """{label: track_ids} for each non-empty bucket source of the ride spec."""
    row = db.execute(
        text("SELECT sources_json FROM dj_mix_specs WHERE name = :name"), {"name": spec}
    ).first()
    sources = json.loads(row[0]) if row and row[0] else []
    store = _buckets()
    lanes: dict[str, list[int]] = {}
    for src in sources:
        if src.get("kind") != "bucket":
            continue
        try:
            b = store.find_bucket(str(src.get("query", "")))
        except ValueError as e:  # ambiguous name: skip that lane, keep the rest
            logger.warning("ride fallback: %s", e)
            continue
        ids = [int(t["track_id"]) for t in (b or {}).get("tracks") or []]
        if ids:
            lanes[str(src.get("label") or b["name"])] = ids
    return lanes


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
