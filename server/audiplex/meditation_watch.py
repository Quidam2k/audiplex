"""Watch meditation folders and keep their Book rows in step (#9295, #5462).

Adds new files, and since #5462 also drops rows whose file is gone, through the
same meditation-scoped scan_library sweep as POST /api/library/scan/meditations.
A sweep only runs on a stable, non-empty snapshot, so an empty or unreadable
folder (a drive hiccup) never reads as "every meditation was deleted".
"""

import asyncio
import logging
from pathlib import Path

from audiplex import database
from audiplex.config import LibraryRoot, get_settings
from audiplex.scanner import scan_library
from audiplex.scanners.meditation import _audio_files

logger = logging.getLogger(__name__)
TICK_S = 60.0


def meditation_roots() -> list[str]:
    return [root.path for root in get_settings().library_roots
            if root.category == "meditation"]


def snapshot(roots: list[str]) -> frozenset | None:
    entries = set()
    for root in roots:
        if not Path(root).is_dir():
            return None
        for path in _audio_files(Path(root)):
            try:
                stat = path.stat()
            except OSError:
                return None
            entries.add((str(path), stat.st_size, stat.st_mtime_ns))
    return frozenset(entries) if entries else None


class MeditationWatch:
    def __init__(self, roots_fn=meditation_roots, scan_fn=None):
        self._roots_fn = roots_fn
        self._scan_fn = scan_fn or self._scan_and_sweep
        self._seen = None
        self._scanned = None

    def tick(self) -> str:
        roots = self._roots_fn()
        if not roots:
            return "no-roots"
        snap = snapshot(roots)
        if snap is None:
            self._seen = None
            return "skip"
        if snap != self._seen:
            self._seen = snap
            return "unstable"
        if snap == self._scanned:
            return "idle"
        try:
            self._scan_fn(roots)
        except Exception:
            logger.exception("Meditation watch scan failed")
            return "error"
        self._scanned = snap
        return "scanned"

    def _scan_and_sweep(self, roots: list[str]) -> None:
        # Re-check right before the sweep: the folder may have emptied since the tick looked.
        if snapshot(roots) is None:
            raise RuntimeError("meditation folder empty or unreadable; not sweeping")
        db = database._SessionLocal()
        try:
            result = scan_library(
                db, [LibraryRoot(path=r, category="meditation") for r in roots],
                get_settings().cover_cache_dir,
            )
            if result.added or result.updated or result.removed:
                logger.info("Meditation scan: added=%s updated=%s removed=%s",
                            result.added, result.updated, result.removed)
            if result.errors:
                logger.warning("Meditation scan errors=%s", result.errors)
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()


async def run_ticker(interval: float = TICK_S) -> None:
    watch = MeditationWatch()
    while True:
        try:
            await asyncio.to_thread(watch.tick)
        except Exception:
            logger.exception("Meditation watch tick failed")
        await asyncio.sleep(interval)
