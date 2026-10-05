"""Server-side stops the DJ can trust (#3505).

On 2026-09-30 at 00:28Z a persona called dj_sleep_timer, the phone (an Aug-21
build) answered `failed: unknown_type`, the tool said "Queued", and the music
played on while the personas kept appending one song per track. Two things were
missing, and both live here, in the FastAPI process, which outlives every MCP
session:

  * A STOP JOB that does the stopping itself, using only commands every phone
    build understands (replace_upcoming when the phone has it, else `pause`,
    plus `volume` steps for a fade), and then VERIFIES it: the job is only
    YES once the device reports playing=false after the stop. Anything else
    is NO with the reason. Modes:
      after_current  trim the tail and let the current song end; if the trim
                     is not acked ok, pause ~1 s before the song's end.
      fade           after `minutes`, ramp volume to 0 over `fade_seconds`,
                     pause, restore the volume (the sleep-timer fallback for
                     phones that don't know sleep_timer).
  * A STOP LATCH. While it is set the command route refuses queue/play_next
    (409) and the DJ pool does not top up, so personas cannot quietly refill a
    queue Todd asked to end. An explicit start (play_now, resume, play_stream,
    play_book, activate) or DELETE clears it, and it expires on its own after
    LATCH_TTL_S so tomorrow's DJ never meets a mystery refusal. It never
    touches Todd's own taps: those never reach the command bus.

tick() is a pure function of (bus, now), driven by run_ticker(); tests call it
with a fake clock.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

LATCH_TTL_S = 3 * 3600.0     # Karen rider (a): the latch expires on its own
TRIM_ACK_WAIT_S = 20.0       # no answer to replace_upcoming in this long -> pause path
PAUSE_LEAD_S = 1.0           # pause this far before the song's end (no-trim path)
END_GRACE_S = 5.0            # trimmed: if still playing this long past the end, pause
VERIFY_WINDOW_S = 20.0       # the device must report playing=false within this
FADE_STEP_S = 1.0            # one volume command per second while fading
TICK_S = 0.5

# An explicit start is a fresh request (from Todd, via a persona): it ends the stop.
CLEARING_CMDS = {"play_now", "resume", "play_stream", "play_book", "activate"}
# What the latch refuses: anything that grows the queue. A replace_upcoming
# that brings tracks back (dj_mix, a pool start) is a refill too (#6913);
# the empty one is the stop's own trim.
REFILL_CMDS = {"queue", "play_next"}


def _diag(record: dict[str, Any]) -> None:
    try:
        from audiplex.playback_bus import _append_diag

        _append_diag("scheduled_stop", record)
    except Exception:
        pass


def _track(state: Optional[dict]) -> dict:
    t = (state or {}).get("track")
    return t if isinstance(t, dict) else {}


class StopController:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.job: Optional[dict[str, Any]] = None
        self.last: Optional[dict[str, Any]] = None  # the most recent finished job
        self.latch: Optional[dict[str, Any]] = None

    # ----- latch -----

    def latch_active(self, now: float) -> bool:
        if self.latch and now >= self.latch["expires_at"]:
            _diag({"event": "latch_expired", "reason": self.latch["reason"]})
            self.latch = None
        return self.latch is not None

    def set_latch(self, reason: str, now: float) -> None:
        self.latch = {"reason": reason, "set_at": now, "expires_at": now + LATCH_TTL_S}
        _diag({"event": "latch_set", "reason": reason})

    def clear(self, why: str, bus=None, now: Optional[float] = None) -> bool:
        """Drop the latch and any pending job. True if there was anything to drop."""
        had = self.latch is not None or self.job is not None
        if self.job is not None:
            self._finish(None, f"cancelled: {why}", bus, now or time.time())
        self.latch = None
        if had:
            _diag({"event": "cleared", "why": why})
        return had

    def gate(self, cmd_type: str, payload: dict, now: float, bus=None) -> Optional[str]:
        """The command route's check: a refusal message, or None to let it through."""
        if cmd_type in CLEARING_CMDS:
            self.clear(f"explicit {cmd_type}", bus, now)
            return None
        refill = cmd_type in REFILL_CMDS or (
            cmd_type == "replace_upcoming" and (payload or {}).get("track_ids"))
        if refill and self.latch_active(now):
            left = int((self.latch["expires_at"] - now) // 60)
            return (f"STOPPED: {self.latch['reason']}. Nothing was queued. Todd asked for the "
                    f"music to stop; the stop holds for another {left} min. If Todd asks for "
                    "music again, use dj_play_now (it lifts the stop) or dj_stop_cancel.")
        return None

    # ----- arming -----

    def arm_after_current(self, bus, now: float) -> dict[str, Any]:
        state = bus.get_state()
        track = _track(state)
        if not state or not track or not state.get("playing"):
            return {"ok": False, "error": "Nothing is playing, so there is no current song to stop after."}
        if (track.get("id") or 0) < 0 or not state.get("duration_ms"):
            return {"ok": False, "error": "The current item has no known end (a stream or a DJ clip). Use dj_pause."}
        self._replace_job(bus, now)
        trim = bus._enqueue("replace_upcoming", {"track_ids": []}, source="scheduled_stop")
        # #6913: 1.0.52+ pauses exactly at the song's end (Media3
        # pauseAtEndOfMediaItems); older builds answer unknown_type and the
        # timed pause below still covers them.
        exact = bus._enqueue("stop_after_current", {"track_id": track.get("id")}, source="scheduled_stop")
        self.job = {
            "mode": "after_current", "phase": "trimming", "armed_at": now,
            "track_id": track.get("id"), "track_title": track.get("title"),
            "trim_cmd_id": trim.id, "trim": "pending", "commands": [trim.id, exact.id],
            "exact_cmd_id": exact.id,
        }
        self._stop_pool()
        self.set_latch(f"stop after '{track.get('title')}' (armed by the DJ)", now)
        _diag({"event": "armed", "mode": "after_current", "track_id": track.get("id")})
        return {"ok": True, **self.public(now, bus)}

    def arm_fade(self, bus, now: float, minutes: float, fade_seconds: float) -> dict[str, Any]:
        if minutes <= 0:
            return {"ok": False, "error": "minutes must be > 0"}
        fade = max(1.0, min(float(fade_seconds), minutes * 60))
        self._replace_job(bus, now)
        self.job = {
            "mode": "fade", "phase": "waiting", "armed_at": now,
            "deadline": now + minutes * 60, "fade_seconds": fade, "commands": [],
        }
        _diag({"event": "armed", "mode": "fade", "minutes": minutes, "fade_seconds": fade})
        return {"ok": True, **self.public(now, bus)}

    def _replace_job(self, bus, now: float) -> None:
        if self.job is not None:
            self._finish(None, "replaced by a new stop", bus, now)

    @staticmethod
    def _stop_pool() -> None:
        try:
            from audiplex.dj_pool import get_pool

            get_pool().stop()
        except Exception as e:
            print(f"[scheduled_stop] pool stop skipped: {e}", flush=True)

    # ----- the clock -----

    def tick(self, bus, now: float) -> None:
        self.latch_active(now)  # expiry
        job = self.job
        if job is None:
            return
        state = bus.get_state()
        if job["phase"] in ("trimming", "await_end"):
            self._tick_after_current(job, bus, state, now)
        elif job["phase"] in ("waiting", "fading"):
            self._tick_fade(job, bus, state, now)
        if self.job is not None and self.job["phase"] == "verifying":
            self._tick_verify(self.job, bus, state, now)

    def _send(self, job: dict, bus, cmd: str, payload: dict) -> int:
        rec = bus._enqueue(cmd, payload, source=f"scheduled_stop:{job['mode']}")  # #3552
        job["commands"].append(rec.id)
        return rec.id

    def _start_verify(self, job: dict, bus, now: float, why: str, pause: bool) -> None:
        if pause:
            job["pause_cmd_id"] = self._send(job, bus, "pause", {})
        job["phase"], job["action_at"], job["action"] = "verifying", now, why
        _diag({"event": "action", "mode": job["mode"], "why": why, "paused": pause})

    def _tick_after_current(self, job: dict, bus, state: Optional[dict], now: float) -> None:
        if job["phase"] == "trimming":
            rec = bus.command(job["trim_cmd_id"])
            if rec is not None and rec.ack_status:
                job["trim"] = "ok" if rec.ack_status == "ok" else f"{rec.ack_status}: {rec.ack_detail}".rstrip(": ")
                job["phase"] = "await_end"
            elif now - job["armed_at"] >= TRIM_ACK_WAIT_S:
                job["trim"] = "no answer"
                job["phase"] = "await_end"
        if not state:
            return
        track = _track(state)
        updated = float(state.get("updated_at") or 0)
        if not state.get("playing") and updated >= job["armed_at"]:
            # Already stopped (the trimmed queue ran out, or someone paused).
            job["action_at"], job["action"] = job["armed_at"], "device stopped on its own"
            job["phase"] = "verifying"
            return
        if track.get("id") != job["track_id"]:
            self._start_verify(job, bus, now, "the song changed before the stop", pause=True)
            return
        pos = float(state.get("position_ms") or 0)
        if state.get("playing") and updated:
            pos += max(0.0, now - updated) * 1000
        remaining_s = (float(state.get("duration_ms") or 0) - pos) / 1000
        trimmed = job["trim"] == "ok"
        if not trimmed and remaining_s <= PAUSE_LEAD_S:
            self._start_verify(job, bus, now, "paused at the song's end (no trim)", pause=True)
        elif trimmed and remaining_s <= -END_GRACE_S:
            self._start_verify(job, bus, now, "still playing past the song's end", pause=True)

    def _tick_fade(self, job: dict, bus, state: Optional[dict], now: float) -> None:
        fade = job["fade_seconds"]
        if job["phase"] == "waiting":
            if now < job["deadline"] - fade:
                return
            vol = (state or {}).get("volume")
            job["start_volume"] = float(vol) if isinstance(vol, (int, float)) and vol > 0 else 1.0
            job["fade_start"], job["last_step_at"] = now, 0.0
            job["phase"] = "fading"
        frac = min(1.0, (now - job["fade_start"]) / fade)
        if frac >= 1.0:
            self._send(job, bus, "volume", {"volume": 0.0})
            self._start_verify(job, bus, now, "faded out and paused", pause=True)
            self._stop_pool()
            self.set_latch("sleep timer ran out (the DJ faded the music out)", now)
            return
        if now - job["last_step_at"] >= FADE_STEP_S:
            job["last_step_at"] = now
            self._send(job, bus, "volume", {"volume": round(job["start_volume"] * (1 - frac), 3)})

    def _tick_verify(self, job: dict, bus, state: Optional[dict], now: float) -> None:
        pause_id = job.get("pause_cmd_id")
        if pause_id is not None:
            rec = bus.command(pause_id)
            if rec is not None and rec.ack_status and rec.ack_status != "ok":
                self._finish("NO", f"the device refused pause: {rec.ack_status} {rec.ack_detail}".strip(), bus, now)
                return
        updated = float((state or {}).get("updated_at") or 0)
        if state and not state.get("playing") and updated >= job["action_at"] - 0.5:
            self._finish("YES", f"{job['action']}; the device reports playing=no", bus, now)
            return
        if now - job["action_at"] >= VERIFY_WINDOW_S:
            said = "still playing" if state and state.get("playing") else "nothing new"
            self._finish("NO", f"{job['action']}, but the device reported {said} "
                               f"within {VERIFY_WINDOW_S:g}s", bus, now)

    def _finish(self, verdict: Optional[str], reason: str, bus, now: float) -> None:
        job = self.job
        if job is None:
            return
        if job["mode"] == "fade" and job.get("start_volume") is not None and bus is not None:
            # Paused (or cancelled mid-fade): put the volume back so the next
            # start isn't silent.
            self._send(job, bus, "volume", {"volume": job["start_volume"]})
        if job.get("exact_cmd_id") and bus is not None:
            # #6913: the phone's one-shot end-of-song pause must not outlive the
            # stop (harmless if it already fired).
            bus._enqueue("cancel_stop_after_current", {}, source="scheduled_stop:finished")
        job.update(verdict=verdict or "CANCELLED", reason=reason, finished_at=now, phase="done")
        _diag({"event": "finished", "mode": job["mode"], "verdict": job["verdict"], "reason": reason})
        self.last, self.job = job, None

    # ----- reporting -----

    def public(self, now: float, bus=None) -> dict[str, Any]:
        job = self.job or self.last
        out: dict[str, Any] = {"active": self.job is not None, "latched": self.latch_active(now)}
        if self.latch:
            out["latch_reason"] = self.latch["reason"]
            out["latch_expires_in_s"] = int(self.latch["expires_at"] - now)
        if job:
            keys = ("mode", "phase", "track_id", "track_title", "trim", "action", "verdict",
                    "reason", "commands", "fade_seconds")
            out["job"] = {k: job[k] for k in keys if k in job}
            if job.get("deadline") and self.job is not None:
                out["job"]["fires_in_s"] = round(job["deadline"] - now, 1)
            if job.get("mode") == "after_current" and self.job is not None and bus is not None:
                s = bus.get_state() or {}
                if _track(s).get("id") == job.get("track_id") and s.get("duration_ms"):
                    pos = float(s.get("position_ms") or 0)
                    if s.get("playing") and s.get("updated_at"):
                        pos += max(0.0, now - float(s["updated_at"])) * 1000
                    out["job"]["ends_in_s"] = round((float(s["duration_ms"]) - pos) / 1000, 1)
        return out


controller = StopController()


async def run_ticker(bus, interval: float = TICK_S) -> None:
    while True:
        try:
            controller.tick(bus, time.time())
        except Exception as e:  # a bad tick must never kill the loop
            print(f"[scheduled_stop] tick failed: {e}", flush=True)
        await asyncio.sleep(interval)
