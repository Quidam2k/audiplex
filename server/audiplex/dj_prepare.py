"""Ahead-of-time DJ breaks (#5648, Todd msg 59058).

"Rendered live" means rendered AHEAD in the DJ voice: when a break is planned for a
song's end, render it minutes early so a failed render can retry "without having
missed the train". Saved clips are the rare exception, never the same ones over and
over; when every try fails the default is a pause.

Flow, one entry per song (pool.state["prepared"], persisted with the pool):

  prepare()  POST /api/playback/pool/prepared, a persona's line for the CURRENT song.
  tick()     every ticker pass (5 s): start the next render try when it is due and
             speech is clear, then insert the clip PREPARE_FIRE_LEAD_S before the
             song ends, after checking it is still the same song and the same next
             track. Never starts a try while Todd talks or a persona line is queued
             or on air (Jarvis's condition 1 on asg 11715).
  on_boundary()  from dj_triggers.on_state at every track change: a song that
             changed before its break fired cancels its pending tries (Karen's
             ask 2), and a failed break whose fallback is a pause pauses now, at the
             boundary (the same boundary-pause shape as a dropped ride outro).

Tries: at prepare, then +30/+60/+120 s (TRY_OFFSETS_S), each capped at TRY_TIMEOUT_S,
none starting later than LAST_TRY_LEAD_S before the song's end. A song too short for
that schedule gets it scaled to fit (Karen's ask 1).

All tries failed (or the next track changed): a bank clip that passes the reuse cap
(none in REUSE_NONE_WITHIN_S, at most REUSE_MAX_PER_30D in 30 days, dj_clip_uses),
else the fallback setting: pause (default) | bank (a bank clip even over the cap,
the pre-#5648 behaviour) | silent. Every try, fire, drop, fallback and pause is a
line in prepare_log_path() (JSONL) and in the server log, so Todd can audit what
failed and on which try; the try_start/try_end lines also let the persona-latency
check pair lines queued during a render against lines that were not.

delivery "over" (#5648, Todd 59058): the break is not queued; it is handed to the external
DJ_OVER_MUSIC_CMD hook at T-30 s. Tries stop at T-40 s. A ready clip goes with --wav; no clip,
or a changed next track, goes with --fallback-say (same text and ids). Exactly one hand-off per
entry, claimed under the lock. A render landing after the hand-off is discarded and logged. An
"over" break never pauses.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex  # #5648
import sqlite3
import subprocess  # #5648
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from audiplex import dj_triggers

logger = logging.getLogger("audiplex.dj_prepare")

TRY_OFFSETS_S = (0.0, 30.0, 60.0, 120.0)
TRY_TIMEOUT_S = 30.0
LAST_TRY_LEAD_S = 25.0       # no try starts later than this before the song's end
PREPARE_FIRE_LEAD_S = 15.0   # the clip goes into the phone's queue this long before the end
MIN_INSERT_LEAD_S = 3.0      # a render landing later than this before the end is too late
TICK_MARGIN_S = 5.0          # one ticker pass: a scaled last try is due this far inside the cutoff
REUSE_NONE_WITHIN_S = 7 * 86400.0
REUSE_MAX_PER_30D = 2
FALLBACKS = ("pause", "bank", "silent")
DEFAULT_FALLBACK = "pause"
PERSONA_QUEUE_STATUSES = ("pending", "processing", "ready")
PERSONA_QUEUE_MAX_AGE_S = 300.0  # an older 'pending' row is a parked line, not one about to play
KEEP_ENTRIES = 20
OPEN = ("preparing", "ready", "held")
NATURAL_END_SLACK_S = 10.0   # a track change this close to the estimated end is the song ending, not a skip
PAUSE_EXPIRE_S = 120.0       # a pause fallback not sent by end + this is dropped (playback stopped meanwhile)
DELIVERIES = ("insert", "over")  # #5648: "insert" = a gap clip into the queue; "over" = handed to the over-music hook
OVER_LAST_TRY_LEAD_S = 40.0  # #5648 over: no render try starts later than this before the song's end
OVER_HANDOFF_LEAD_S = 30.0   # #5648 over: the hand-off (clip or fallback say) goes out at T-30
OVER_HOOK_TIMEOUT_S = 120.0  # #5648 over: the external hook is a child process; its rc is logged, not awaited

_LOCK = threading.RLock()
_TASKS: dict[str, Any] = {}  # entry id -> the in-flight try (an asyncio.Task in the server)
_QUEUE_WARNED = False


# ----- small helpers -----


def prepare_log_path() -> Path:
    return Path(os.environ.get("AUDIPLEX_DJ_PREPARE_LOG")
                or dj_triggers._server_dir() / "data" / "dj_prepare_log.jsonl")


def _log(event: str, entry: Optional[dict] = None, **fields: Any) -> None:
    """One audit line (JSONL) plus the server log. Never raises."""
    rec = {"at": time.time(), "event": event}
    if entry is not None:
        rec.update(entry_id=entry.get("id"), track_id=entry.get("for_track"))
    rec.update(fields)
    level = logging.WARNING if event in ("fallback", "fallback_pause", "dropped") else logging.INFO
    logger.log(level, "dj_prepare %s %s", event, {k: v for k, v in rec.items() if k != "at"})
    try:
        path = prepare_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    except OSError:
        logger.exception("dj_prepare log write failed")


def _entries(pool) -> list[dict]:
    return pool.state.setdefault("prepared", [])


def settings(pool=None) -> dict:
    pool = dj_triggers._get_pool(pool)
    s = pool.state.get("prepare_settings") or {}
    fb = s.get("fallback") if s.get("fallback") in FALLBACKS else DEFAULT_FALLBACK
    return {"fallback": fb}


def set_settings(pool=None, fallback: Optional[str] = None) -> dict:
    pool = dj_triggers._get_pool(pool)
    if fallback is not None:
        if fallback not in FALLBACKS:
            raise ValueError(f"fallback must be one of {', '.join(FALLBACKS)}")
        pool.state.setdefault("prepare_settings", {})["fallback"] = fallback
        pool._persist()
    return settings(pool)


def _next_track(state: dict) -> Optional[int]:
    idx = state.get("queue_index") or 0
    for item in state.get("queue") or []:
        if isinstance(item, dict) and (item.get("index") or 0) > idx:
            qid = item.get("id")
            if isinstance(qid, int) and qid > 0:
                return qid
    return None


def _current(state: Optional[dict]) -> Optional[int]:
    track = (state or {}).get("track") or {}
    cur = track.get("id") if isinstance(track, dict) else None
    return cur if isinstance(cur, int) else None


def song_end_epoch(state: Optional[dict], now: float) -> Optional[float]:
    """When the current song ends, from the last state report; None when unknown."""
    if not state or not state.get("playing"):
        return None
    dur, pos = state.get("duration_ms") or 0, state.get("position_ms") or 0
    if dur <= 0:
        return None
    at = float(state.get("updated_at") or now)
    return at + (dur - pos) / 1000.0


def last_try_lead(delivery: str) -> float:
    """#5648: how far before the song's end the last render try may start."""
    return OVER_LAST_TRY_LEAD_S if delivery == "over" else LAST_TRY_LEAD_S


def try_due_times(t0: float, end: Optional[float], delivery: str = "insert") -> list[float]:  # #5648
    """Absolute due time of each try. A song too short for +120 s before
    T-last_try_lead gets the offsets scaled to fit (Karen's ask 1)."""  # #5648
    span = TRY_OFFSETS_S[-1]
    scale = 1.0
    if end is not None:
        avail = end - last_try_lead(delivery) - TICK_MARGIN_S - t0  # #5648
        scale = max(0.0, min(1.0, avail / span)) if span else 1.0
    return [t0 + off * scale for off in TRY_OFFSETS_S]


# ----- the speech gate for starting a try -----


def _pantheon_db() -> str:
    return os.environ.get("AUDIPLEX_PANTHEON_DB") or dj_triggers.audiplex_paths.pantheon("data", "pantheon.db")


def persona_line_queued(now: Optional[float] = None) -> bool:
    """A persona line waiting to be spoken (Pantheon speech_queue). Read-only; a
    missing or unreadable DB is fail-open (the speech-state file still gates)."""
    global _QUEUE_WARNED
    now = time.time() if now is None else now
    since = datetime.fromtimestamp(now - PERSONA_QUEUE_MAX_AGE_S, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    path = _pantheon_db()
    try:
        if not Path(path).is_file():
            raise OSError("no Pantheon DB")
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.5)
        try:
            marks = ",".join("?" * len(PERSONA_QUEUE_STATUSES))
            row = con.execute(
                f"SELECT 1 FROM speech_queue WHERE status IN ({marks}) AND timestamp >= ? LIMIT 1",
                (*PERSONA_QUEUE_STATUSES, since),
            ).fetchone()
        finally:
            con.close()
        return row is not None
    except (OSError, sqlite3.Error) as e:
        if not _QUEUE_WARNED:
            _QUEUE_WARNED = True
            logger.info("persona queue unreadable (%s): the speech-state file alone gates DJ prepare renders", e)
        return False


def speech_busy(now: Optional[float] = None) -> Optional[str]:
    """Why a try must not start now, or None. Todd talking, a persona on air, or queued."""
    now = time.time() if now is None else now
    speech = dj_triggers.read_speech_state()
    if speech.get("talking"):
        return "todd_talking"
    if dj_triggers.agent_audio_busy(speech, now):  # current_claim_holder is awareness-only in Pantheon: not a gate
        return "persona_on_air"
    if persona_line_queued(now):
        return "persona_queued"
    return None


# ----- prepare -----


def prepare(bus, say: str, title: str = "DJ break", for_track: Optional[int] = None,
            expect_next: Optional[int] = None, agent: Optional[str] = None,
            fallback_kind: Optional[str] = None, artist: Optional[str] = None,
            pool=None, now: Optional[float] = None, delivery: str = "insert") -> dict:  # #5648
    """File a break for the end of the CURRENT song. The first try starts on the
    next tick (immediately when the ticker loop is running). delivery (#5648):
    "insert" = a gap clip into the queue (pauses if every try fails); "over" = the
    external over-music hook at T-OVER_HANDOFF_LEAD_S, with a say fallback, never a pause."""
    pool = dj_triggers._get_pool(pool)
    now = time.time() if now is None else now
    if delivery not in DELIVERIES:
        raise ValueError(f"delivery must be one of {', '.join(DELIVERIES)}")  # #5648
    st = bus.get_state()
    cur = _current(st)
    if not st or not st.get("playing") or cur is None:
        return {"prepared": False, "reason": "nothing playing"}
    if cur <= 0:
        return {"prepared": False, "reason": "not a music track (stream or DJ break)"}
    if for_track is not None and int(for_track) != cur:
        return {"prepared": False, "reason": f"track {for_track} is not playing (now {cur})"}
    say = (say or "").strip()
    if not say:
        return {"prepared": False, "reason": "no text"}
    end = song_end_epoch(st, now)
    entry = {
        "id": f"p{int(now * 1000)}",
        "say": say,
        "title": title or "DJ break",
        "agent": agent,
        "for_track": cur,
        "expect_next": int(expect_next) if expect_next is not None else _next_track(st),
        "fallback_kind": fallback_kind,
        "artist": artist,
        "created_at": now,
        "end_at": end,
        "delivery": delivery,  # #5648
        "due": try_due_times(now, end, delivery),  # #5648
        "tries": [],
        "rendering": False,
        "status": "preparing",
        "done": False,
    }
    with _LOCK:
        replaced = []
        for e in _entries(pool):
            if e.get("for_track") == cur and (e.get("status") in OPEN or e.get("status") == "pause_at_boundary"):
                _cancel(e, "replaced")
                replaced.append(e["id"])
        entries = _entries(pool)
        entries.append(entry)
        del entries[:-KEEP_ENTRIES]
        pool._persist()
    _log("prepared", entry, delivery=delivery, end_in_s=None if end is None else round(end - now, 1),  # #5648
         replaced=replaced)
    return {"prepared": True, "id": entry["id"], "for_track": cur, "expect_next": entry["expect_next"],
            "delivery": delivery,  # #5648
            "song_ends_in_s": None if end is None else round(end - now, 1),
            "tries_due_in_s": [round(d - now, 1) for d in entry["due"]], "replaced": replaced}


def _kill_task(entry: dict) -> None:
    """Cancel an entry's in-flight try (a skipped song wastes at most one render)."""
    task = _TASKS.pop(entry["id"], None)
    if task is not None and hasattr(task, "cancel") and not task.done():
        loop = dj_triggers._loop  # on_boundary runs on a threadpool thread: cancel on the task's loop
        try:
            on_loop = asyncio.get_running_loop() is loop
        except RuntimeError:
            on_loop = False
        if loop is not None and loop.is_running() and not on_loop:
            loop.call_soon_threadsafe(task.cancel)
        else:
            task.cancel()
    entry["rendering"] = False


def cancel_all(pool=None) -> None:
    """The pool was reset (dj_pool.stop): no in-flight try outlives its entry."""
    with _LOCK:
        for e in _entries(dj_triggers._get_pool(pool)):
            if not e.get("done"):
                e.update(status="dropped", done=True, drop_reason="pool_stopped")
        for eid in list(_TASKS):
            _kill_task({"id": eid})


def _cancel(entry: dict, reason: str) -> None:
    entry.update(status="dropped", done=True, drop_reason=reason)
    _kill_task(entry)
    _log("dropped", entry, reason=reason, tries=len(entry.get("tries") or []))


# ----- render tries -----


async def _render(text: str, title: str) -> dict:
    """The one server-side DJ render (dj_triggers.render_say); tests swap this."""
    return await dj_triggers.render_say(text, title)


async def run_try(entry: dict, pool, n: int) -> None:
    """Render try n for entry; records the outcome on the entry."""
    started = time.time()
    _log("try_start", entry, n=n)
    ok, err, clip = False, None, None
    try:
        clip = await asyncio.wait_for(_render(entry["say"], entry["title"]), TRY_TIMEOUT_S)
        ok = True
    except asyncio.CancelledError:
        err = "cancelled"
        raise
    except asyncio.TimeoutError:
        err = f"timeout after {TRY_TIMEOUT_S:g}s"
    except Exception as e:  # render_say raises RuntimeError on TTS failure
        err = str(e) or type(e).__name__
    finally:
        ended = time.time()
        late = False  # #5648
        with _LOCK:
            entry["rendering"] = False
            entry["tries"].append({"n": n, "started_at": started, "ended_at": ended, "ok": ok, "error": err})
            if ok and entry.get("status") in ("preparing",):
                entry.update(status="ready", clip_id=clip["clip_id"],
                             clip_duration=clip.get("duration_seconds"), rendered_at=clip.get("rendered_at"),  # #5648
                             wav_path=clip.get("wav_path"))
            elif ok:  # #5648 a render that lands after the break was resolved (fallback sent, or cancelled)
                late = True  # #5648
            _TASKS.pop(entry["id"], None)
            pool._persist()
        if ok and (late or entry.get("delivery") != "over"):  # #5648
            _discard_wav((clip or {}).get("wav_path"))  # the insert path plays the stored clip, not the wav
        _log("try_end", entry, n=n, ok=ok, error=err, secs=round(ended - started, 2))
        if late:  # #5648
            _log("late_render_discarded", entry, n=n, status=entry.get("status"))


def _discard_wav(path: Optional[str]) -> None:  # #5648
    if path:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            logger.exception("dj_prepare could not remove render wav %s", path)


# ----- the reuse cap (dj_clip_uses) -----


def _session():
    """A DB session for dj_clip_uses; None when there is no DB (tests patch this)."""
    from audiplex import playback_bus

    return playback_bus._pool_session()


def reuse_allowed(db, voice: str, key: str, now: float) -> bool:
    from audiplex.models import DjClipUse

    since30 = datetime.fromtimestamp(now - 30 * 86400, tz=timezone.utc).replace(tzinfo=None)
    since7 = datetime.fromtimestamp(now - REUSE_NONE_WITHIN_S, tz=timezone.utc).replace(tzinfo=None)
    uses = [u for (u,) in db.query(DjClipUse.used_at).filter(
        DjClipUse.voice == voice, DjClipUse.clip_key == key, DjClipUse.used_at >= since30)]
    return len(uses) < REUSE_MAX_PER_30D and not any(u >= since7 for u in uses)


def _record_use(db, voice: str, key: str, now: float, reason: str) -> None:
    from audiplex.models import DjClipUse

    db.add(DjClipUse(clip_key=key, voice=voice, reason=reason,
                     used_at=datetime.fromtimestamp(now, tz=timezone.utc).replace(tzinfo=None)))
    db.commit()


def _bank():
    try:
        from audiplex_mcp import dj_clip_bank
    except ImportError:  # the server runs from server/, like render_say
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from audiplex_mcp import dj_clip_bank
    return dj_clip_bank


def _bank_clip(entry: dict) -> Optional[tuple[str, str]]:
    return _bank().fallback_clip(entry.get("fallback_kind"), entry.get("artist"))


def _bank_voice() -> str:
    return _bank().manifest_voice() or ""


def _store_bank_clip(path: str) -> tuple[int, Optional[float]]:
    from audiplex.routers.dj_voice import store_clip_bytes

    p = Path(path)
    return store_clip_bytes(p.read_bytes(), p.suffix.lower())


def saved_clip(entry: dict, now: float, over_cap: bool = False) -> Optional[dict]:
    """A bank clip for this break that passes the reuse cap (or any, over_cap=True),
    stored as a playable clip and its use recorded. None on a miss."""
    bank = _bank_clip(entry)
    if bank is None:
        return None
    bank_id, path = bank
    voice = _bank_voice()
    key = f"bank:{bank_id}"
    db = _session()
    if db is None and not over_cap:
        return None  # no ledger, no proof it has not been overplayed
    try:
        if not over_cap and not reuse_allowed(db, voice, key, now):
            _log("reuse_capped", entry, clip_key=key, voice=voice)
            return None
        clip_id, duration = _store_bank_clip(path)
        if db is not None:
            _record_use(db, voice, key, now, f"{entry.get('id')} track {entry.get('for_track')}")
        return {"clip_id": clip_id, "clip_duration": duration, "clip_key": key, "voice": voice}
    finally:
        if db is not None:
            db.close()


# ----- firing and fallback -----


def _fire_clip(entry: dict, bus, now: float, clip_id: int, duration: Optional[float], how: str) -> None:
    cue = {"id": entry["id"], "clip_id": int(clip_id), "clip_title": entry["title"], "clip_duration": duration,
           "actions": [{"type": "insert_clip", "clip_id": int(clip_id)}]}
    ids = dj_triggers.execute_actions(cue, bus, now)
    entry.update(status="fired", done=True, fired_at=now, fired_clip_id=int(clip_id), fired_how=how,
                 command_ids=ids)
    _log("fired", entry, how=how, clip_id=int(clip_id), command_ids=ids)


def _failure_summary(entry: dict) -> str:
    tries = entry.get("tries") or []
    if not tries:
        return "no try could start (speech never cleared)"
    last = tries[-1]
    return f"{len(tries)} tries failed; last (try {last['n']}): {last.get('error')}"


def _resolve_fallback(entry: dict, bus, pool, now: float, why: str) -> None:
    """Every try failed, or the next track changed: saved clip under the cap, else the setting."""
    fb = settings(pool)["fallback"]
    failed = _failure_summary(entry) if why == "render_failed" else why
    clip = None
    if fb != "silent":
        try:
            clip = saved_clip(entry, now)
            if clip is None and fb == "bank":
                clip = saved_clip(entry, now, over_cap=True)
        except Exception:
            logger.exception("dj_prepare saved-clip lookup failed")
            clip = None
    if clip is not None:
        _log("fallback", entry, used="saved_clip", clip_key=clip["clip_key"], failed=failed, setting=fb)
        _fire_clip(entry, bus, now, clip["clip_id"], clip.get("clip_duration"), how="saved_clip")
        return
    if fb == "pause":
        entry.update(status="pause_at_boundary", done=False, failed=failed)
        _log("fallback", entry, used="pause", failed=failed, setting=fb)
        return
    entry.update(status="failed", done=True, failed=failed)
    _log("fallback", entry, used="silent", failed=failed, setting=fb)


def _no_clip_reason(e: dict) -> str:  # #5648
    if e.get("status") == "ready":
        return "the rendered clip has no wav file"
    if e.get("rendering"):
        return f"render still in flight at T-{OVER_HANDOFF_LEAD_S:g}"
    return _failure_summary(e)


# ----- delivery "over" (#5648): hand-off to the external over-music hook -----


def over_cmd() -> str:  # #5648
    """The over-music hook command (DJ_OVER_MUSIC_CMD), '' when unset. Argument contract
    appended to it, in this order (values are separate argv items, never shell-joined):
      --wav <path> | --fallback-say   (the clip, or no clip: say the text live)
      --text <say> --end-epoch <float> --track-id <int> --next-track-id <int|''> --persona <agent|''>
    """
    configured = os.environ.get("DJ_OVER_MUSIC_CMD")  # #5648
    if configured is not None:  # #5648 set (even to '') = the operator's choice; '' turns the hook off
        return configured.strip()  # #5648
    return dj_triggers.pantheon_script_cmd("dj_over_music.py")  # #5648 default, like DJ_BRIDGE_CMD (#5457)


def over_argv(cmd: str, e: dict, end_epoch: float, wav: Optional[str]) -> list[str]:  # #5648
    parts = shlex.split(cmd, posix=False)  # Windows-friendly, as the bridge hook is parsed
    argv = [p[1:-1] if len(p) >= 2 and p[0] == p[-1] == '"' else p for p in parts]
    argv += ["--wav", wav] if wav else ["--fallback-say"]
    nxt = e.get("expect_next")
    argv += [
        "--text", e["say"],
        "--end-epoch", repr(float(end_epoch)),
        "--track-id", str(e["for_track"]),
        "--next-track-id", "" if nxt is None else str(nxt),
        "--persona", e.get("agent") or "",
    ]
    return argv


def _spawn_over_hook(argv: list[str], entry_id: str) -> None:  # #5648
    """Run the hook as a child of the server. The ticker never waits on it; its rc and
    output are logged when it ends."""
    def _run() -> None:
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=OVER_HOOK_TIMEOUT_S)
            _log("over_hook_done", entry_id=entry_id, rc=r.returncode,
                 stdout=r.stdout[-300:], stderr=r.stderr[-300:])
        except Exception as exc:  # a hook failure never reaches the ticker
            _log("over_hook_failed", entry_id=entry_id, error=repr(exc))

    threading.Thread(target=_run, name=f"dj-over-{entry_id}", daemon=True).start()


def _over_handoff(e: dict, st: dict, now: float, out: dict) -> None:  # #5648
    """delivery "over" at T-OVER_HANDOFF_LEAD_S: exactly one hand-off per entry. The claim
    is one state transition under _LOCK, so a late render or a second tick cannot send
    another. A ready clip goes with --wav; no clip, or the next track changed, goes with
    --fallback-say. Never pauses."""
    nxt = _next_track(st)
    with _LOCK:
        if e.get("done") or e.get("status") not in OPEN:
            return
        cmd = over_cmd()
        if not cmd:
            e.update(status="no_hook", done=True)
            _log("handoff_skipped", e, reason="DJ_OVER_MUSIC_CMD unset")
            out["fallback"].append(e["id"])
            return
        changed_next = e.get("expect_next") is not None and nxt != e["expect_next"]
        wav = e.get("wav_path")
        if e.get("status") == "ready" and wav and not changed_next:
            e.update(status="handed_off", done=True, handed_at=now, handoff="clip")
            clip = True
        else:
            why = (f"next track changed ({e.get('expect_next')} -> {nxt})" if changed_next
                   else f"no clip by T-{OVER_HANDOFF_LEAD_S:g}: {_no_clip_reason(e)}")
            e.update(status="fallback_sent", done=True, handed_at=now, handoff="fallback", failed=why)
            clip = False
            _discard_wav(wav)  # a clip that landed too late for this hand-off is not played
        end = song_end_epoch(st, now) or e.get("end_at") or now
        argv = over_argv(cmd, e, end, wav if clip else None)
        if clip:
            _log("handoff", e, how="clip", wav=wav, argv=argv)
            out["fired"].append(e["id"])
        else:
            _log("fallback", e, used="over_say", failed=e.get("failed"), argv=argv)
            out["fallback"].append(e["id"])
    _spawn_over_hook(argv, e["id"])


def _outro_owns(pool, track_id: int) -> bool:
    outro = pool.state.get("outro")
    return isinstance(outro, dict) and (outro.get("trigger") or {}).get("track_id") == track_id


def tick(bus, pool=None, now: Optional[float] = None,
         spawn: Optional[Callable[[Any], Any]] = None) -> dict:
    """One pass over open entries: start due tries, fire ready clips near the end."""
    pool = dj_triggers._get_pool(pool)
    now = time.time() if now is None else now
    out: dict = {"started": [], "fired": [], "dropped": [], "fallback": []}
    st = bus.get_state()
    cur = _current(st)
    with _LOCK:
        changed = False
        for e in _entries(pool):
            if e.get("done") or e.get("status") not in OPEN:
                continue
            if cur is not None and cur > 0 and cur != e["for_track"]:
                _cancel(e, "song_changed")  # the boundary hook usually got here first
                out["dropped"].append(e["id"])
                changed = True
                continue
            if cur != e["for_track"]:
                continue  # a DJ clip or a stream between songs, or no report: wait
            end = song_end_epoch(st, now)
            if end is not None:
                e["end_at"] = end
            left = None if end is None else end - now
            if e["rendering"] and e["id"] not in _TASKS:
                e["rendering"] = False  # a restart lost the task: count it and move on
                e["tries"].append({"n": len(e["tries"]), "started_at": None, "ended_at": now,
                                   "ok": False, "error": "lost (server restart)"})
                changed = True

            if e["status"] == "preparing" and not e["rendering"]:
                n = len(e["tries"])
                last_ok = left is None or left >= last_try_lead(e.get("delivery"))  # #5648
                if n < len(TRY_OFFSETS_S) and last_ok and now >= e["due"][n]:
                    busy = speech_busy(now)
                    if busy:
                        if e.get("waiting") != busy:
                            e["waiting"] = busy
                            _log("try_waiting", e, n=n, reason=busy)
                    else:
                        e.pop("waiting", None)
                        e["rendering"] = True
                        coro = run_try(e, pool, n)
                        task = (spawn or _spawn)(coro)
                        if task is not None:
                            _TASKS[e["id"]] = task
                        out["started"].append((e["id"], n))
                        changed = True

            over = e.get("delivery") == "over"  # #5648
            if left is None or left > (OVER_HANDOFF_LEAD_S if over else PREPARE_FIRE_LEAD_S):  # #5648
                continue
            # Fire time: within PREPARE_FIRE_LEAD_S of the end (OVER_HANDOFF_LEAD_S for "over"). #5648
            if _outro_owns(pool, e["for_track"]):
                _cancel(e, "outro_owns_end")
                out["dropped"].append(e["id"])
                changed = True
                continue
            if over:  # #5648
                _over_handoff(e, st, now, out)
                changed = True
                continue
            nxt = _next_track(st)
            if e.get("expect_next") is not None and nxt != e["expect_next"]:
                _kill_task(e)
                why = f"next track changed ({e['expect_next']} -> {nxt})"
                _resolve_fallback(e, bus, pool, now, why)
                out["fallback"].append(e["id"])
                changed = True
                continue
            if e["status"] in ("ready", "held"):
                speech = dj_triggers.read_speech_state()
                if speech.get("talking") or dj_triggers.agent_audio_busy(speech, now):
                    if e["status"] != "held":
                        e["status"] = "held"
                        _log("held", e, reason="todd_talking" if speech.get("talking") else "persona_on_air")
                        changed = True
                    continue
                if left < MIN_INSERT_LEAD_S:
                    continue  # too late to land cleanly; the boundary hook drops it
                _fire_clip(e, bus, now, e["clip_id"], e.get("clip_duration"), how="prepared")
                out["fired"].append(e["id"])
                changed = True
            elif e["status"] == "preparing":
                if e["rendering"] and left >= MIN_INSERT_LEAD_S:
                    continue  # a try is still in flight and could land in time
                _kill_task(e)  # past T-LAST_TRY_LEAD_S no new try starts: this is the end
                _resolve_fallback(e, bus, pool, now, "render_failed")
                out["fallback"].append(e["id"])
                changed = True
        if changed:
            pool._persist()
    return out


def _spawn(coro):
    """tick() runs inside dj_triggers.run_ticker, so there is always a running loop."""
    return asyncio.get_running_loop().create_task(coro)


def _ended_naturally(e: dict, now: float) -> bool:
    end = e.get("end_at")
    return end is None or now >= end - NATURAL_END_SLACK_S


def on_boundary(cur: int, bus, pool, now: Optional[float] = None) -> None:
    """A track change (dj_triggers.on_state). Cancel breaks for a song that is gone;
    send the pause fallback for a break that failed at its song's NATURAL end.
    A skip never pauses, a DJ clip or stream between songs is waited out, and a
    pause still unsent well past the song's end (playback stopped) expires."""
    now = time.time() if now is None else now
    with _LOCK:
        changed = False
        for e in _entries(pool):
            if e.get("for_track") == cur:
                continue
            if e.get("status") == "pause_at_boundary":
                end = e.get("end_at")
                if end is not None and now > end + PAUSE_EXPIRE_S:
                    e.update(status="dropped", done=True, drop_reason="pause_expired")
                    _log("dropped", e, reason="pause_expired", now_track=cur)
                elif cur <= 0:
                    continue  # a DJ clip or stream: pause at the next real song
                elif not _ended_naturally(e, now):
                    e.update(status="dropped", done=True, drop_reason="skipped")
                    _log("dropped", e, reason="skipped (no pause: Todd moved on)", now_track=cur)
                else:
                    bus._enqueue("pause", {}, source="dj_prepare:fallback_pause")
                    e.update(status="paused", done=True, paused_at=now)
                    _log("fallback_pause", e, failed=e.get("failed"), now_track=cur)
                changed = True
            elif not e.get("done") and e.get("status") in OPEN:
                if not _ended_naturally(e, now):
                    reason = "skipped"
                elif e.get("status") == "held":
                    reason = "held_through_end (Todd talking or a persona line on air)"
                elif e.get("status") == "ready":
                    reason = "too_late"
                else:
                    reason = "song_changed"
                _cancel(e, reason)
                changed = True
        if changed:
            pool._persist()


def status(pool=None) -> dict:
    pool = dj_triggers._get_pool(pool)
    keep = ("id", "for_track", "expect_next", "status", "say", "agent", "tries", "drop_reason",
            "failed", "fired_how", "fired_clip_id", "created_at", "end_at", "delivery", "handoff")  # #5648
    return {"settings": settings(pool),
            "entries": [{k: e.get(k) for k in keep} for e in _entries(pool)]}


def open_for(track_id: int, pool=None) -> Optional[dict]:
    """The open (or already fired) prepared break for a song, for the watcher's T-20 backstop."""
    pool = dj_triggers._get_pool(pool)
    for e in reversed(_entries(pool)):
        if e.get("for_track") == track_id and e.get("status") not in ("dropped",):
            return e
    return None
