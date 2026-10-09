"""Measure a queued track's loudness on the spot when it has none (#3504).

The phone's per-track gain (#7109) needs tracks.loudness_lufs. Every library
track is swept today, but a fresh ingest has none until the next sweep, and an
unmeasured track plays with no gain at all (#7387). So any track a playback-bus
command queues without a value is measured right then: one ffmpeg ebur128 pass,
CPU only, below-normal priority, in a background thread so enqueue never blocks.
Queued tracks usually play minutes later, so the value is there when the phone
fetches the track. Failures leave the track unmeasured, i.e. the old behaviour.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
from typing import Callable, Iterable

_INTEGRATED = re.compile(r"I:\s+(-?\d+(?:\.\d+)?)\s+LUFS")
_BELOW_NORMAL = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0x00004000)
_in_flight: set[int] = set()
_lock = threading.Lock()


def measure_lufs(path: str, ffmpeg: str = "ffmpeg", timeout: float = 600) -> float | None:
    """EBU R128 integrated loudness of a file, or None if ffmpeg can't say."""
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-nostats", "-threads", "1", "-i", path,
         "-map", "0:a:0", "-af", "ebur128", "-f", "null", "-"],
        capture_output=True, timeout=timeout,
        creationflags=_BELOW_NORMAL if os.name == "nt" else 0,
    )
    found = _INTEGRATED.findall(proc.stderr.decode("utf-8", errors="replace"))
    return float(found[-1]) if found else None  # the summary's I: is the last one


def measure_missing(ids: Iterable[int], session_factory: Callable, measure=measure_lufs) -> dict[int, float]:
    """Measure and store loudness for the given music tracks that have none."""
    from audiplex.models import Track

    done: dict[int, float] = {}
    db = session_factory()
    try:
        rows = (db.query(Track).filter(Track.id.in_(list(ids)), Track.loudness_lufs.is_(None))
                .all())
        for track in rows:
            if getattr(track, "content_kind", "music") not in (None, "music") or not track.file_path:
                continue
            try:
                lufs = measure(track.file_path)
            except Exception as e:
                print(f"[loudness_live] {track.id}: {e}", flush=True)
                continue
            if lufs is not None:
                track.loudness_lufs = lufs
                db.commit()
                done[track.id] = lufs
    finally:
        db.close()
    return done


def schedule(ids: Iterable[int], session_factory: Callable) -> threading.Thread | None:
    """Measure unmeasured tracks among ids in a daemon thread; None when nothing to do."""
    with _lock:
        todo = [int(t) for t in ids if isinstance(t, int) and t > 0 and t not in _in_flight]
        _in_flight.update(todo)
    if not todo:
        return None

    def run():
        try:
            measure_missing(todo, session_factory)
        except Exception as e:
            print(f"[loudness_live] skipped: {e}", flush=True)
        finally:
            with _lock:
                _in_flight.difference_update(todo)

    thread = threading.Thread(target=run, name="loudness_live", daemon=True)
    thread.start()
    return thread
