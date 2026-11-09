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
_TRUE_PEAK = re.compile(r"True peak:\s*\n\s*Peak:\s+(-?\d+(?:\.\d+)?|-inf)\s+dBFS")  # #5683
_BELOW_NORMAL = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0x00004000)
_in_flight: set[int] = set()
INGEST = True  # #5683 measure new items after a scan; tests turn it off (conftest)
_ingest_busy = threading.Event()  # #5683 one ingest thread at a time
_lock = threading.Lock()


def parse_summary(stderr: str) -> tuple[float | None, float | None]:  # #5683
    """(integrated LUFS, true peak dBTP) from ffmpeg ebur128 output; None where absent."""
    found = _INTEGRATED.findall(stderr)
    peaks = [p for p in _TRUE_PEAK.findall(stderr) if p != "-inf"]
    return (float(found[-1]) if found else None,  # the summary's I: is the last one
            float(peaks[-1]) if peaks else None)


def measure_loudness(path: str, ffmpeg: str = "ffmpeg", timeout: float = 600) -> tuple[float | None, float | None]:  # #5683
    """(EBU R128 integrated loudness, true peak) of a file: one ffmpeg pass, 1 thread, below-normal."""
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-nostats", "-threads", "1", "-i", path,
         "-map", "0:a:0", "-af", "ebur128=peak=true", "-f", "null", "-"],  # #5683 peak=true
        capture_output=True, timeout=timeout,
        creationflags=_BELOW_NORMAL if os.name == "nt" else 0,
    )
    return parse_summary(proc.stderr.decode("utf-8", errors="replace"))


def measure_lufs(path: str, ffmpeg: str = "ffmpeg", timeout: float = 600) -> float | None:
    """EBU R128 integrated loudness of a file, or None if ffmpeg can't say."""
    return measure_loudness(path, ffmpeg, timeout)[0]  # #5683


def measure_missing(ids: Iterable[int], session_factory: Callable, measure=measure_loudness) -> dict[int, float]:  # #5683
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
            lufs, peak = lufs if isinstance(lufs, tuple) else (lufs, None)  # #5683 (lufs, true peak)
            if peak is not None:
                track.true_peak_dbtp = peak  # #5683
            if lufs is not None:
                track.loudness_lufs = lufs
                db.commit()
                done[track.id] = lufs
    finally:
        db.close()
    return done


def measure_missing_books(ids: Iterable[int], session_factory: Callable, measure=measure_loudness) -> dict[int, float]:  # #5683
    """Measure and store loudness + true peak for the given books (meditations) that have none."""
    from audiplex.models import Book

    done: dict[int, float] = {}
    db = session_factory()
    try:
        for book in db.query(Book).filter(Book.id.in_(list(ids)), Book.loudness_lufs.is_(None)).all():
            if not book.file_path or not os.path.isfile(book.file_path):
                continue
            try:
                lufs, peak = measure(book.file_path)
            except Exception as e:
                print(f"[loudness_live] book {book.id}: {e}", flush=True)
                continue
            if lufs is not None:
                book.loudness_lufs, book.true_peak_dbtp = lufs, peak
                db.commit()
                done[book.id] = lufs
    finally:
        db.close()
    return done


def schedule_ingest(session_factory: Callable, limit: int = 200) -> threading.Thread | None:  # #5683
    """After a scan: measure unmeasured music tracks and meditations in ONE background
    thread (one ffmpeg at a time, below-normal), so a new item is levelled before it
    plays. Audiobooks are left to the idle-gated scripts/loudness_backfill.py (hours long)."""
    if not INGEST or _ingest_busy.is_set():
        return None
    from audiplex.models import Book, Track
    db = session_factory()
    try:
        tracks = [r[0] for r in db.query(Track.id).filter(Track.loudness_lufs.is_(None),
                                                          Track.content_kind == "music").limit(limit)]
        books = [r[0] for r in db.query(Book.id).filter(Book.loudness_lufs.is_(None),
                                                        Book.category == "meditation").limit(limit)]
    finally:
        db.close()
    if not tracks and not books:
        return None
    _ingest_busy.set()

    def run():
        try:
            measure_missing(tracks, session_factory)
            measure_missing_books(books, session_factory)
        except Exception as e:
            print(f"[loudness_live] ingest skipped: {e}", flush=True)
        finally:
            _ingest_busy.clear()

    thread = threading.Thread(target=run, name="loudness_ingest", daemon=True)
    thread.start()
    return thread


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
