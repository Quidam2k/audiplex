"""Persist Todd's DJ stop latch (#4011).

The latch is lifted ONLY by lift() with Todd's quote (dj_stop_lift).
There is no timer. It is untouched by resume, DELETE /scheduled-stop,
or the after-current latch.
"""

import json
import os
import time
from pathlib import Path

LATCH_PATH = Path(__file__).resolve().parent.parent / "data" / "todd_stop.json"  # #4011

START_CMDS = {
    "play_now",
    "resume",
    "queue",
    "play_next",
    "replace_upcoming",
    "play_stream",
    "play_book",
    "activate",
    "announce",
    "bed_play",
}


def _read_state() -> dict | None:  # #4011
    try:
        state = json.loads(LATCH_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception:
        return {
            "latched": True,
            "reason": "latch file unreadable - failing closed",
            "todd_quote": "",
            "set_at": 0,
        }
    if isinstance(state, dict):
        return state
    return {"latched": True, "reason": "latch file malformed - failing closed",
            "todd_quote": "", "set_at": 0}  # #4011


def _write_state(state: dict) -> None:  # #4011
    LATCH_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = LATCH_PATH.with_suffix(".json.tmp")
    with temporary_path.open("w", encoding="utf-8") as stream:
        json.dump(state, stream, ensure_ascii=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary_path, LATCH_PATH)


def _history(state: dict | None) -> list:  # #4011
    history = state.get("history") if state is not None else None
    return list(history) if isinstance(history, list) else []


def read_latch() -> dict | None:  # #4011
    state = _read_state()
    return state if state is not None and state.get("latched") is True else None


def is_latched() -> bool:  # #4011
    return read_latch() is not None


def set_latch(reason: str, todd_quote: str, now: float) -> dict:  # #4011
    if not isinstance(todd_quote, str) or not todd_quote.strip():
        raise ValueError("todd_quote required")

    history = _history(_read_state())
    history.append({
        "event": "set",
        "at": now,
        "quote": todd_quote,
        "reason": reason,
    })
    state = {
        "latched": True,
        "reason": reason,
        "todd_quote": todd_quote,
        "set_at": now,
        "history": history,
    }
    _write_state(state)
    return state


def lift(todd_quote: str, now: float) -> dict:  # #4011
    if not isinstance(todd_quote, str) or not todd_quote.strip():
        raise ValueError("todd_quote required")

    prior = read_latch()
    if prior is None:
        return {"lifted": False, "reason": "not latched"}

    history = _history(prior)
    history.append({"event": "lift", "at": now, "quote": todd_quote})
    state = {
        "latched": False,
        "history": history,
        "lifted_at": now,
        "lift_quote": todd_quote,
    }
    _write_state(state)
    return {"lifted": True, **state}


def _refusal_text(state: dict) -> str:  # #4011
    try:
        stopped_at = time.strftime("%H:%M", time.localtime(state.get("set_at", 0)))
    except (TypeError, ValueError, OverflowError, OSError):
        stopped_at = time.strftime("%H:%M", time.localtime(0))
    quote = state.get("todd_quote") or "no words recorded"
    return (
        f"Todd stopped the DJ at {stopped_at} ('{quote}'). "
        "Only Todd lifts it \u2014 dj_stop_lift with his words."
    )


def refusal(now: float) -> str | None:  # #4011
    state = read_latch()
    return None if state is None else _refusal_text(state)


def gate(  # #4011
    cmd_type: str,
    payload: dict | None,
    source: str,
    owner: str,
    now: float,
) -> str | None:
    state = read_latch()
    if state is None or source == owner:
        return None
    if cmd_type not in START_CMDS:
        return None
    if cmd_type == "replace_upcoming" and not (payload or {}).get("track_ids"):
        return None
    return _refusal_text(state)
