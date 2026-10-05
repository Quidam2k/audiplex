"""DJ trigger engine: cues that fire at track boundaries, the clock, or (later) a geofence.

Tickets #5480 (cue clips render ahead of time), #5499 (Westminster clock chimes),
#5515 (ride-end outro).

A trigger ({kind: track_end|track_start|clock|geofence, ...}) maps to actions
(insert_clip, queue_track, bed_chime, pause). Two feeds drive it:

  * on_state(): the PlaybackBus state-report hook, the same place the DJ pool
    tops up. It sees each track change once (a "boundary").
  * clock_tick(): a server ticker every few seconds (quarter chimes, clock cues).

Geofence triggers have a schema and a matcher here and no feed yet (#3261).

Placement rules. A clip is always inserted with the phone's `announce` command in
mode "next", i.e. right after whatever is playing. So "track_end of Y" fires the
moment Y becomes current (the clip lands at Y's end) and "track_start of X" fires
when X's predecessor is current (the clip lands right before X). Nothing here ever
interrupts a song or uses play_now; a "pause" action means pause AFTER the clip.

Hold guard (hold_guard(), the one place this is decided). Before any cue fires it
reads the speech-state file: while Todd is talking/typing the cue is held and
retried at the next boundary, and dropped after MAX_HELD_BOUNDARIES missed ones.
A REACTIVE clip (rendered within REACTIVE_WINDOW_S of its first check, i.e. about
the conversation) rendered before Todd last spoke is stale and dropped. PLANNED cues
(a spec's notes, or anything rendered long ahead) skip that check: hold +
drop-after-MAX_HELD_BOUNDARIES only (#5544). Only a missing or unreadable file is
fail-open.

State lives in the DJ pool's state dict (persisted with it): pending_cues (shared
with DJPool), outro, trigger_last_track, chime_settings, chime_runtime.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger("audiplex.dj_triggers")

TRIGGER_KINDS = ("track_end", "track_start", "clock", "geofence")
ACTION_TYPES = ("insert_clip", "queue_track", "bed_chime", "pause")
MAX_HELD_BOUNDARIES = 2
# A cue rendered within this many seconds of its first check is REACTIVE (#5544).
REACTIVE_WINDOW_S = 600
CHIME_LATE_LIMIT_S = 20.0
CHIME_MINUTES = (0, 15, 30, 45)
DEFAULT_CHIME_SETTINGS = {"enabled": True, "volume": 0.12, "hour_strikes": False}
STATE_FRESH_S = 30.0          # a renderer state older than this is not "playing"
PAUSE_LEAD_S = 0.1            # send the after-clip pause this much before the clip ends
CHIME_STOP_PAD_S = 1.5        # bed_stop goes out sound_seconds + this after bed_play
CLIP_PIN_GRACE_S = 3600.0     # a fired cue's clip stays pinned: the phone fetches it when it plays
DEFAULT_SPEECH_STATE_FILE = "Q:/Pantheon/data/runtime/speech_state.json"
PAUSED_STATES = ("paused", "paused_late", "paused_fallback", "paused_no_outro")

_SPEECH_WARNED = False
_FAILOPEN_WARNED = False
_client_base_url: Optional[str] = None
_loop: Optional[asyncio.AbstractEventLoop] = None


def _get_pool(pool):
    if pool is not None:
        return pool
    from audiplex.dj_pool import get_pool

    return get_pool()


def _server_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def chime_dir() -> Path:
    return Path(os.environ.get("AUDIPLEX_CHIME_DIR") or _server_dir() / "assets" / "chimes")


def generated_chime_dir() -> Path:
    return Path(os.environ.get("AUDIPLEX_CHIME_CACHE_DIR") or _server_dir() / "data" / "chimes_generated")


def reset_warnings() -> None:
    """Re-arm the once-per-process warnings (tests)."""
    global _SPEECH_WARNED, _FAILOPEN_WARNED
    _SPEECH_WARNED = False
    _FAILOPEN_WARNED = False


# ----- validation -----


def _int(v: Any, what: str) -> int:
    if isinstance(v, bool):
        raise ValueError(f"{what} must be an integer")
    try:
        return int(v)
    except (TypeError, ValueError):
        raise ValueError(f"{what} must be an integer") from None


def _float(v: Any, what: str) -> float:
    if isinstance(v, bool):
        raise ValueError(f"{what} must be a number")
    try:
        return float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{what} must be a number") from None


def validate_trigger(trigger: dict) -> dict:
    """Normalized copy of a trigger; ValueError when it cannot work."""
    if not isinstance(trigger, dict):
        raise ValueError("trigger must be an object")
    kind = trigger.get("kind")
    if kind not in TRIGGER_KINDS:
        raise ValueError(f"trigger kind must be one of {TRIGGER_KINDS}")
    if kind in ("track_end", "track_start"):
        tid = _int(trigger.get("track_id"), "track_id")
        if tid <= 0:
            raise ValueError("track_id must be a positive track id")
        return {"kind": kind, "track_id": tid}
    if kind == "clock":
        if trigger.get("minutes") is not None:
            minutes = trigger["minutes"]
            if not isinstance(minutes, (list, tuple)) or not minutes:
                raise ValueError("clock minutes must be a non-empty list")
            norm = sorted({_int(m, "minute") for m in minutes})
            if any(m < 0 or m > 59 for m in norm):
                raise ValueError("clock minutes must be 0-59")
            return {"kind": kind, "minutes": norm}
        at = trigger.get("at")
        try:
            hh, mm = str(at).split(":")
            h, m = int(hh), int(mm)
        except (TypeError, ValueError):
            raise ValueError("clock trigger needs minutes [..] or at 'HH:MM'") from None
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError("clock at must be a valid 24h HH:MM")
        return {"kind": kind, "at": f"{h:02d}:{m:02d}"}
    # geofence
    lat = _float(trigger.get("lat"), "lat")
    lon = _float(trigger.get("lon"), "lon")
    radius = _float(trigger.get("radius_m"), "radius_m")
    on = trigger.get("on", "enter")
    if not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise ValueError("lat/lon out of range")
    if radius <= 0:
        raise ValueError("radius_m must be > 0")
    if on not in ("enter", "exit", "inside"):
        raise ValueError("geofence on must be enter, exit or inside")
    return {"kind": kind, "lat": lat, "lon": lon, "radius_m": radius, "on": on}


def validate_actions(actions: list[dict]) -> list[dict]:
    """Normalized copy of an action list; ValueError when it cannot work."""
    if not isinstance(actions, list) or not actions:
        raise ValueError("actions must be a non-empty list")
    out = []
    for a in actions:
        if not isinstance(a, dict) or a.get("type") not in ACTION_TYPES:
            raise ValueError(f"action type must be one of {ACTION_TYPES}")
        t = a["type"]
        if t == "insert_clip":
            out.append({"type": t, "clip_id": _int(a.get("clip_id"), "clip_id")})
        elif t == "queue_track":
            tid = _int(a.get("track_id"), "track_id")
            if tid <= 0:
                raise ValueError("queue_track needs a positive track_id")
            out.append({"type": t, "track_id": tid})
        elif t == "bed_chime":
            clip = a.get("clip")
            if not isinstance(clip, str) or not clip:
                raise ValueError("bed_chime needs a clip name")
            vol = _float(a.get("volume", DEFAULT_CHIME_SETTINGS["volume"]), "volume")
            if not 0 <= vol <= 1:
                raise ValueError("bed_chime volume must be 0-1")
            entry = {"type": t, "clip": clip, "volume": vol}
            if a.get("duration_s") is not None:
                dur = _float(a["duration_s"], "duration_s")
                if dur <= 0:
                    raise ValueError("bed_chime duration_s must be > 0")
                entry["duration_s"] = dur
            out.append(entry)
        else:
            out.append({"type": "pause"})
    if any(a["type"] == "pause" for a in out) and not any(a["type"] == "insert_clip" for a in out):
        raise ValueError("pause is only allowed after an insert_clip (never mid-song)")
    return out


# ----- cue model -----


def is_engine_cue(cue: dict) -> bool:
    """Cues with a clip or explicit actions belong to this engine; play_track/say-only
    cues stay with the legacy DJPool.top_up matcher."""
    return isinstance(cue, dict) and bool(cue.get("clip_id") or cue.get("actions"))


def cue_actions(cue: dict) -> list[dict]:
    if cue.get("actions"):
        return list(cue["actions"])
    actions = []
    if cue.get("play_track"):
        actions.append({"type": "queue_track", "track_id": int(cue["play_track"])})
    if cue.get("clip_id"):
        actions.append({"type": "insert_clip", "clip_id": int(cue["clip_id"])})
    return actions


def _all_cues(ps: dict) -> list[dict]:
    cues = [c for c in ps.get("pending_cues") or [] if isinstance(c, dict)]
    if isinstance(ps.get("outro"), dict):
        cues.append(ps["outro"])
    return cues


# ----- geofence (schema + matcher only; no feed yet, #3261) -----


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def in_geofence(trigger: dict, lat: float, lon: float) -> bool:
    return haversine_m(trigger["lat"], trigger["lon"], lat, lon) <= trigger["radius_m"]


def match_geofence(trigger: dict, was_inside: Optional[bool], lat: float, lon: float) -> bool:
    inside = in_geofence(trigger, lat, lon)
    on = trigger.get("on", "enter")
    if on == "inside":
        return inside
    if on == "enter":
        return inside and was_inside is False
    return (not inside) and was_inside is True


# ----- clock -----


def clock_slot(trigger: dict, now_dt: datetime) -> Optional[datetime]:
    """The latest scheduled time <= now_dt for a clock trigger (naive local time)."""
    base = now_dt.replace(second=0, microsecond=0)
    if trigger.get("minutes") is not None:
        wanted = set(trigger["minutes"])
        for step in range(61):
            cand = base - timedelta(minutes=step)
            if cand.minute in wanted:
                return cand
        return None
    at = trigger.get("at")
    if not at:
        return None
    try:
        h, m = (int(x) for x in str(at).split(":"))
    except ValueError:
        return None
    cand = base.replace(hour=h, minute=m)
    return cand if cand <= now_dt else cand - timedelta(days=1)


# ----- speech state + the hold guard -----


def _parse_ts(v: Any) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    return None


def speech_state_path() -> Path:
    return Path(os.environ.get("DJ_SPEECH_STATE_FILE") or DEFAULT_SPEECH_STATE_FILE)


def read_speech_state() -> dict:
    """{talking, last_spoke_at, readable}. Only a missing/unreadable file is fail-open."""
    global _SPEECH_WARNED
    path = speech_state_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
    except (OSError, ValueError) as e:
        if not _SPEECH_WARNED:
            _SPEECH_WARNED = True
            logger.warning("speech state %s unreadable (%s): treating Todd as not talking", path, e)
        return {"talking": False, "last_spoke_at": None, "readable": False}
    return {
        "talking": any(data.get(k) for k in ("stt_active", "talk_active", "composing")),
        "last_spoke_at": _parse_ts(data.get("todd_last_spoke_at")),
        "readable": True,
    }


def _drop(cue: dict, reason: str) -> str:
    cue.update(status="dropped", done=True, drop_reason=reason)
    logger.info("cue %s dropped: %s", cue.get("id"), reason)
    return "dropped"


def is_reactive(cue: dict, now: Optional[float] = None) -> bool:
    """Reactive = about the conversation, so it can go stale. Decided once, on the
    cue's first check, and stored as cue["reactive"] so aging never flips it (#5544).
    planned=True (spec cues) is never reactive; otherwise rendered within
    REACTIVE_WINDOW_S of that first check."""
    if cue.get("planned"):
        return False
    if isinstance(cue.get("reactive"), bool):
        return cue["reactive"]
    now = time.time() if now is None else now
    rendered = _parse_ts(cue.get("rendered_at"))
    cue["reactive"] = rendered is not None and now - rendered <= REACTIVE_WINDOW_S
    return cue["reactive"]


def hold_guard(cue: dict, speech: Optional[dict] = None) -> str:
    """'fire' | 'held' | 'dropped' for a cue about to fire. The only hold guard."""
    global _FAILOPEN_WARNED
    speech = speech if speech is not None else read_speech_state()
    last = speech.get("last_spoke_at")
    rendered = _parse_ts(cue.get("rendered_at"))
    reactive = is_reactive(cue)  # #5544: only reactive cues can go stale
    if last is None:
        if not _FAILOPEN_WARNED:
            _FAILOPEN_WARNED = True
            logger.info("staleness check fail-open: no todd_last_spoke_at in the speech state")
    elif reactive and rendered is not None and last > rendered:
        return _drop(cue, "stale")
    if speech.get("talking"):
        cue["held_boundaries"] = int(cue.get("held_boundaries") or 0) + 1
        if cue["held_boundaries"] >= MAX_HELD_BOUNDARIES:
            return _drop(cue, "held_too_long")
        cue["status"] = "held"
        return "held"
    return "fire"


# ----- scheduling -----


def set_loop(loop: Optional[asyncio.AbstractEventLoop]) -> None:
    global _loop
    _loop = loop


def _schedule(delay_s: float, fn: Callable[[], Any]) -> None:
    """Run fn after delay_s: on the ticker's loop when there is one, else a timer thread."""
    delay_s = max(0.0, float(delay_s))
    loop = _loop
    if loop is not None and loop.is_running():
        loop.call_soon_threadsafe(loop.call_later, delay_s, fn)
        return
    timer = threading.Timer(delay_s, fn)
    timer.daemon = True
    timer.start()


# ----- actions -----


def _announce_payload(clip_id: int, title: Optional[str], duration: Optional[float]) -> dict:
    # mode is always "next": the clip lands between songs, never over one.
    return {
        "clip_id": int(clip_id),
        "clip_url": f"/api/dj/clips/{int(clip_id)}",
        "title": title or "DJ break",
        "duration_seconds": duration,
        "mode": "next",
    }


def execute_actions(cue: dict, bus, now: float) -> list[int]:
    """Send a cue's actions to the phone; returns the command ids."""
    actions = cue_actions(cue)
    ids: list[int] = []
    from audiplex.scheduled_stop import controller as stop_controller

    # #6913: a stop Todd asked for also holds against cue refills.
    latched = stop_controller.latch_active(time.time())
    for a in actions:
        if a.get("type") == "queue_track" and not latched:
            ids.append(bus._enqueue("play_next", {"track_ids": [int(a["track_id"])]},
                                    source="dj_triggers:cue").id)
    # After play_next so the clip (also inserted right after the current item) plays first.
    for a in actions:
        if a.get("type") == "insert_clip" and not latched:
            payload = _announce_payload(a["clip_id"], cue.get("clip_title"), cue.get("clip_duration"))
            ids.append(bus._enqueue("announce", payload, source="dj_triggers:cue").id)
    for a in actions:
        if a.get("type") == "bed_chime":
            base = chime_base_url()
            if base is None:
                logger.warning("cue %s: bed_chime skipped, no client base URL known yet", cue.get("id"))
                continue
            rec = bus._enqueue("bed_play", {
                "url": f"{base}/api/dj/chimes/{a['clip']}",
                "volume": float(a.get("volume", DEFAULT_CHIME_SETTINGS["volume"])),
                "title": "DJ chime",
            })
            ids.append(rec.id)
            _schedule(a.get("duration_s") or 3.0, lambda: bus._enqueue("bed_stop", {}))
    if any(a.get("type") == "pause" for a in actions):
        cue["pause_state"] = "armed"
    return ids


def _fire(cue: dict, bus, now: float) -> None:
    ids = execute_actions(cue, bus, now)
    cue.update(status="fired", done=True, fired_at=now, command_ids=ids)
    logger.info("cue %s fired (commands %s)", cue.get("id"), ids)


# ----- track boundary feed -----


def on_state(state: dict, bus, pool=None, now: Optional[float] = None) -> dict:
    """Evaluate track cues once per track change on the renderer's state report."""
    result: dict = {"boundary": False, "fired": [], "held": [], "dropped": []}
    if not isinstance(state, dict):
        return result
    track = state.get("track") or {}
    cur = track.get("id") if isinstance(track, dict) else None
    if not isinstance(cur, int) or not state.get("playing"):
        return result
    pool = _get_pool(pool)
    now = time.time() if now is None else now
    ps = pool.state
    if ps.get("trigger_last_track") == cur:
        return result
    ps["trigger_last_track"] = cur
    result["boundary"] = True

    _check_pause_watch(state, bus, pool, now)
    if cur < 0:  # a stream (-1) or a DJ voice break: never fire over one
        pool._persist()
        return result

    idx = state.get("queue_index") or 0
    next_id = None
    for item in state.get("queue") or []:
        if not isinstance(item, dict):
            continue
        qid = item.get("id")
        if (item.get("index") or 0) > idx and isinstance(qid, int) and qid > 0:
            next_id = qid
            break

    candidates = [
        c for c in _all_cues(ps)
        if is_engine_cue(c) and not c.get("done")
        and (c.get("trigger") or {}).get("kind") in ("track_end", "track_start")
    ]
    speech = None
    for c in candidates:
        trig = c.get("trigger") or {}
        status = c.get("status", "pending")
        if status == "pending":
            matched = (trig.get("kind") == "track_end" and trig.get("track_id") == cur) or (
                trig.get("kind") == "track_start" and next_id is not None and trig.get("track_id") == next_id
            )
            if not matched:
                continue
        elif status != "held":
            continue
        if speech is None:
            speech = read_speech_state()
        decision = hold_guard(c, speech)
        if decision == "fire":
            _fire(c, bus, now)
            result["fired"].append(c.get("id"))
        else:
            result[decision].append(c.get("id"))
            if decision == "dropped" and c.get("outro"):
                # The ride is still over: skip the words, keep the stop. A song
                # has only just started, so this is a boundary pause, not a cut.
                bus._enqueue("pause", {}, source="dj_triggers:outro_dropped")  # #3552
                c["pause_state"] = "paused_no_outro"
                logger.warning("outro dropped (%s): paused without it", c.get("drop_reason"))
    pool._persist()
    return result


def _check_pause_watch(state: dict, bus, pool, now: float) -> None:
    """Pause after a cue's clip (the outro): timed from the clip's reported position."""
    track = state.get("track") or {}
    cur = track.get("id")
    for c in _all_cues(pool.state):
        ps = c.get("pause_state")
        if ps not in ("armed", "pausing"):
            continue
        if cur < 0 and track.get("title") == c.get("clip_title"):
            c["clip_seen"] = True
            if ps == "armed":
                dur_ms = state.get("duration_ms") or 0
                pos_ms = state.get("position_ms") or 0
                if dur_ms > 0:
                    remaining = (dur_ms - pos_ms) / 1000.0
                else:
                    remaining = float(c.get("clip_duration") or 0) - pos_ms / 1000.0
                c["pause_state"] = "pausing"
                _schedule(max(0.0, remaining - PAUSE_LEAD_S), lambda c=c: _send_pause(c, bus, pool))
        elif cur > 0 and cur != (c.get("trigger") or {}).get("track_id"):
            # A real track started and our pause never went out (report lag, or the
            # clip never showed): stop now, a second into the song, not at its end.
            bus._enqueue("pause", {}, source="dj_triggers:outro_late")  # #3552
            c["pause_state"] = "paused_late" if c.get("clip_seen") else "paused_fallback"
            logger.warning("cue %s: pause sent late (%s)", c.get("id"), c["pause_state"])


def _send_pause(cue: dict, bus, pool) -> None:
    try:
        live = cue is pool.state.get("outro") or any(cue is c for c in pool.state.get("pending_cues") or [])
        if cue.get("pause_state") != "pausing" or not live:
            return
        bus._enqueue("pause", {}, source="dj_triggers:outro")  # #3552
        cue["pause_state"] = "paused"
        pool._persist()
        logger.info("cue %s: paused after its clip", cue.get("id"))
    except Exception:
        logger.exception("after-clip pause failed")


# ----- ride-end outro (#5515) -----


def arm_outro(bus, clip_id: int, title: str, duration_seconds: Optional[float] = None,
              rendered_at: Any = None, agent: Optional[str] = None, say: Optional[str] = None,
              pool=None, now: Optional[float] = None) -> dict:
    """A one-shot track_end cue on the CURRENT track: outro clip after it, then pause."""
    pool = _get_pool(pool)
    now = time.time() if now is None else now
    st = bus.get_state()
    track = (st or {}).get("track") or {}
    cur = track.get("id") if isinstance(track, dict) else None
    if not st or not st.get("playing") or not isinstance(cur, int):
        return {"armed": False, "reason": "nothing playing"}
    if cur <= 0:
        return {"armed": False, "reason": "not a music track (stream or DJ break)"}
    cue = {
        "id": "outro",
        "outro": True,
        "trigger": {"kind": "track_end", "track_id": cur},
        "actions": [{"type": "insert_clip", "clip_id": int(clip_id)}, {"type": "pause"}],
        "clip_id": int(clip_id),
        "clip_title": title,
        "clip_duration": duration_seconds,
        "rendered_at": rendered_at if rendered_at is not None else now,
        "agent": agent,
        "say": say,
        "status": "pending",
        "done": False,
        "held_boundaries": 0,
        "armed_at": now,
    }
    pool.state["outro"] = cue
    # The current track's boundary is now: fire immediately, the clip lands at its end.
    pool.state["trigger_last_track"] = cur
    if hold_guard(cue) == "fire":
        _fire(cue, bus, now)
    pool._persist()
    return {
        "armed": True,
        "status": cue["status"],
        "track_id": cur,
        "clip_id": int(clip_id),
        "title": title,
        "command_ids": cue.get("command_ids", []),
        "drop_reason": cue.get("drop_reason"),
    }


def disarm_outro(pool=None) -> bool:
    pool = _get_pool(pool)
    outro = pool.state.get("outro")
    if isinstance(outro, dict) and outro.get("pause_state") not in PAUSED_STATES:
        pool.state["outro"] = None
        pool._persist()
        return True
    return False


# ----- clip pinning (dj_voice's prune must keep these) -----


def pinned_clip_ids(pool=None, now: Optional[float] = None) -> set[int]:
    try:
        pool = _get_pool(pool)
        now = time.time() if now is None else now
        pinned: set[int] = set()
        for c in _all_cues(pool.state):
            fired_at = c.get("fired_at")
            recent = isinstance(fired_at, (int, float)) and now - fired_at <= CLIP_PIN_GRACE_S
            if c.get("done") and not recent:
                continue
            if c.get("clip_id"):
                pinned.add(int(c["clip_id"]))
            for a in c.get("actions") or []:
                if isinstance(a, dict) and a.get("type") == "insert_clip" and a.get("clip_id"):
                    pinned.add(int(a["clip_id"]))
        return pinned
    except Exception:
        logger.exception("pinned_clip_ids failed")
        return set()


# ----- clock chimes (#5499) -----


def chime_settings(pool=None) -> dict:
    pool = _get_pool(pool)
    return {**DEFAULT_CHIME_SETTINGS, **(pool.state.get("chime_settings") or {})}


def set_chime_settings(pool=None, enabled: Optional[bool] = None, volume: Optional[float] = None,
                       hour_strikes: Optional[bool] = None) -> dict:
    pool = _get_pool(pool)
    s = dict(pool.state.get("chime_settings") or {})
    if enabled is not None:
        s["enabled"] = bool(enabled)
    if volume is not None:
        s["volume"] = min(1.0, max(0.0, float(volume)))
    if hour_strikes is not None:
        s["hour_strikes"] = bool(hour_strikes)
    pool.state["chime_settings"] = s
    pool._persist()
    return chime_settings(pool)


def note_client_base_url(url: str) -> None:
    """The phone's own base URL (as seen on its long-poll): the bed player needs an
    absolute URL, unlike announce clips which the phone resolves itself."""
    global _client_base_url
    if isinstance(url, str) and url.startswith("http"):
        _client_base_url = url.rstrip("/")


def chime_base_url() -> Optional[str]:
    env = os.environ.get("AUDIPLEX_PUBLIC_URL")
    if env:
        return env.rstrip("/")
    return _client_base_url


def chime_clip_for(slot_dt: datetime, hour_strikes: bool) -> Optional[dict]:
    try:
        manifest = json.loads((chime_dir() / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if slot_dt.minute == 0 and hour_strikes:
        try:
            from audiplex import chime_synth

            h = slot_dt.hour % 12 or 12
            _, sound = chime_synth.ensure_hour_clip(h, generated_chime_dir())
            return {"name": f"hour_{h:02d}", "sound_seconds": float(sound)}
        except Exception:
            logger.exception("hour-strike chime unavailable, using the plain quarter")
    entry = manifest.get(f"q{slot_dt.minute:02d}")
    if not isinstance(entry, dict) or "sound_seconds" not in entry:
        return None
    return {"name": f"q{slot_dt.minute:02d}", "sound_seconds": float(entry["sound_seconds"])}


def bed_busy(bus, rt: dict, now: float) -> bool:
    """True while our own chime rings or a bed someone else started (sleep engine) is on."""
    if rt.get("active_until") and now < rt["active_until"]:
        return True
    ours = set(rt.get("chime_command_ids") or [])
    for rec in reversed(list(bus._commands.values())):
        if rec.type == "bed_stop":
            return False
        if rec.type == "bed_play":
            if rec.id in ours:
                return False
            return rec.status != "failed"
    return False


def _check_chime_ack(bus, pool, rt: dict) -> None:
    cmd_id = rt.get("last_command_id")
    if not cmd_id or rt.get("unsupported"):
        return
    rec = bus.command(cmd_id)
    if rec is not None and rec.ack_status == "unknown_type":
        msg = (f"CHIMES UNSUPPORTED: the phone acked bed_play with unknown_type ({rec.ack_detail}); "
               "chimes are disabled for this DJ session")
        logger.error(msg)
        print(f"[dj_triggers] {msg}", flush=True)
        rt.update(unsupported=True, unsupported_detail=rec.ack_detail, last_result="unsupported")
        pool._persist()


def clock_tick(bus, pool=None, now: Optional[float] = None) -> dict:
    """One ticker pass: clock cues, then the quarter chime if one is due."""
    pool = _get_pool(pool)
    now = time.time() if now is None else now
    rt = pool.state.setdefault("chime_runtime", {})
    out: dict = {"chime": "none", "reason": "", "cues": []}
    _check_chime_ack(bus, pool, rt)
    now_dt = datetime.fromtimestamp(now)

    if pool.is_active():
        for c in pool.state.get("pending_cues") or []:
            if not is_engine_cue(c) or c.get("done") or (c.get("trigger") or {}).get("kind") != "clock":
                continue
            slot = clock_slot(c["trigger"], now_dt)
            if slot is None or slot.isoformat() == c.get("last_slot"):
                continue
            c["last_slot"] = slot.isoformat()
            if now - slot.timestamp() <= CHIME_LATE_LIMIT_S and hold_guard(c) == "fire":
                _fire(c, bus, now)
                out["cues"].append(c.get("id"))
        if out["cues"]:
            pool._persist()

    def finish(chime: str, reason: str, record: Optional[str] = None) -> dict:
        if record is not None:
            rt["last_result"] = record
            pool._persist()
        out.update(chime=chime, reason=reason)
        return out

    if not pool.is_active():
        return finish("skipped", "pool inactive")
    settings = chime_settings(pool)
    if not settings.get("enabled"):
        return finish("skipped", "disabled")
    if rt.get("unsupported"):
        return finish("skipped", "unsupported")

    slot = clock_slot({"kind": "clock", "minutes": list(CHIME_MINUTES)}, now_dt)
    key = slot.isoformat()
    if rt.get("last_slot") == key:
        return finish("none", "already handled")
    if now - slot.timestamp() > CHIME_LATE_LIMIT_S:
        rt["last_slot"] = key  # drop, never delay
        return finish("dropped", "late", "dropped: late")

    st = bus.get_state()
    track = (st or {}).get("track") or {}
    tid = track.get("id") if isinstance(track, dict) else None
    fresh = st is not None and now - float(st.get("updated_at") or 0) <= STATE_FRESH_S
    if not (st and st.get("playing") and fresh and isinstance(tid, int) and tid > 0):
        return finish("skipped", "not playing")  # slot left open: a start inside the window still chimes

    rt["last_slot"] = key
    if read_speech_state()["talking"]:
        return finish("dropped", "talking", "dropped: talking")
    if bed_busy(bus, rt, now):
        return finish("skipped", "bed busy", "skipped: bed busy")
    clip = chime_clip_for(slot, bool(settings.get("hour_strikes")))
    if clip is None:
        logger.warning("chime skipped: no clip for %s in %s", key, chime_dir())
        return finish("skipped", "no clip", "skipped: no clip")
    base = chime_base_url()
    if base is None:
        logger.warning("chime skipped: the phone's base URL is not known yet (set AUDIPLEX_PUBLIC_URL)")
        return finish("skipped", "no base url", "skipped: no base url")

    rec = bus._enqueue("bed_play", {
        "url": f"{base}/api/dj/chimes/{clip['name']}",
        "volume": float(settings.get("volume", DEFAULT_CHIME_SETTINGS["volume"])),
        "title": "Westminster chime",
    })
    stop_after = clip["sound_seconds"] + CHIME_STOP_PAD_S
    rt.update(last_command_id=rec.id, active_until=now + stop_after)
    rt["chime_command_ids"] = (list(rt.get("chime_command_ids") or []) + [rec.id])[-20:]
    _schedule(stop_after, lambda: _chime_stop(bus, pool, rec.id))
    return finish("fired", clip["name"], f"fired {clip['name']}")


def _chime_stop(bus, pool, cmd_id: int) -> None:
    try:
        rt = pool.state.setdefault("chime_runtime", {})
        ours = set(rt.get("chime_command_ids") or [])
        newer_bed = any(
            rec.type == "bed_play" and rec.id > cmd_id and rec.id not in ours
            for rec in bus._commands.values()
        )
        if not newer_bed:  # a sleep bed that started meanwhile is not ours to stop
            bus._enqueue("bed_stop", {})
        rt["active_until"] = None
        pool._persist()
    except Exception:
        logger.exception("chime bed_stop failed")


async def run_ticker(bus, interval: float = 5.0) -> None:
    """The clock feed: started from the app lifespan, cancelled on shutdown."""
    set_loop(asyncio.get_running_loop())
    while True:
        try:
            clock_tick(bus)
        except Exception:
            logger.exception("dj trigger clock tick failed")
        await asyncio.sleep(interval)


# ----- status -----


def status(pool=None) -> dict:
    pool = _get_pool(pool)
    rt = pool.state.get("chime_runtime") or {}
    outro = pool.state.get("outro")
    outro_view = None
    if isinstance(outro, dict):
        outro_view = {
            "id": outro.get("id"),
            "status": outro.get("status"),
            "pause_state": outro.get("pause_state"),
            "track_id": (outro.get("trigger") or {}).get("track_id"),
            "clip_id": outro.get("clip_id"),
            "title": outro.get("clip_title"),
            "say": outro.get("say"),
            "agent": outro.get("agent"),
            "drop_reason": outro.get("drop_reason"),
        }
    return {
        "chimes": {
            **chime_settings(pool),
            "unsupported": bool(rt.get("unsupported")),
            "last_result": rt.get("last_result"),
            "last_slot": rt.get("last_slot"),
            "base_url_known": chime_base_url() is not None,
        },
        "chimes_unsupported": bool(rt.get("unsupported")),
        "outro": outro_view,
        "cues": [
            {
                "id": c.get("id"),
                "trigger": c.get("trigger"),
                "say": c.get("say"),
                "status": c.get("status", "pending"),
                "held_boundaries": c.get("held_boundaries", 0),
                "clip_id": c.get("clip_id"),
                "agent": c.get("agent"),
            }
            for c in pool.state.get("pending_cues") or []
            if isinstance(c, dict) and not c.get("done")
        ],
    }


# ----- server-side render for the outro {say} path -----


async def render_say(text: str, title: str) -> dict:
    """Synthesize `text` with the same backend MCP dj_announce uses and store the clip."""
    try:
        from audiplex_mcp import tts_backend
    except ImportError:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from audiplex_mcp import tts_backend

    try:
        path = await tts_backend.synthesize(text)
    except (tts_backend.TtsNotConfigured, tts_backend.TtsFailed) as e:
        raise RuntimeError(str(e)) from e
    try:
        from audiplex.routers.dj_voice import store_clip_bytes

        clip_id, duration = store_clip_bytes(path.read_bytes(), path.suffix.lower())
    finally:
        path.unlink(missing_ok=True)
    return {"clip_id": clip_id, "duration_seconds": duration, "voice": tts_backend.voice(),
            "rendered_at": time.time(), "title": title}
