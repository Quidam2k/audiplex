"""Audiplex — self-hosted audiobook server."""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from audiplex.config import get_settings
from audiplex.database import get_db, init_db
from audiplex.routers import app as app_router
from audiplex.routers import dj_learn as dj_learn_router  # #4057
from audiplex.routers import (
    auth_router,
    dj_voice,
    library,
    music,
    music_video,
    playback,
    progress,
    streaming,
    web,
)
from audiplex.scanner import scan_library

logger = logging.getLogger("audiplex")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: create tables and optionally scan library."""
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()

    logger.info("Starting Audiplex server")
    init_db(settings.database_url)

    if settings.scan_on_startup:
        logger.info("Running startup library scan...")
        db_gen = get_db()
        db = next(db_gen)
        try:
            result = scan_library(db, settings.library_roots, settings.cover_cache_dir)
            logger.info(
                f"Scan complete: {result.added} added, {result.updated} updated, "
                f"{result.removed} removed, {len(result.errors)} errors"
            )
        finally:
            try:
                next(db_gen)
            except StopIteration:
                pass

    # DJ trigger clock feed: quarter chimes + clock cues (#5499).
    from audiplex import dj_triggers
    from audiplex.playback_bus import bus

    ticker = asyncio.create_task(dj_triggers.run_ticker(bus))
    # #3505: verified stop-after-current / fade-then-pause jobs.
    from audiplex import scheduled_stop

    stop_ticker = asyncio.create_task(scheduled_stop.run_ticker(bus))
    _resume_music_video_worker(settings)
    try:
        yield
    finally:
        ticker.cancel()
        stop_ticker.cancel()
        dj_triggers.set_loop(None)


def _resume_music_video_worker(settings):
    """#6172: a restart mid-render picks the queue back up (finished clips are kept)."""
    from pathlib import Path

    from audiplex.database import _SessionLocal
    from audiplex.models import MusicVideoJob
    from audiplex.music_video import worker

    db = _SessionLocal()
    try:
        active = db.query(MusicVideoJob).filter(MusicVideoJob.status.in_(worker.ACTIVE)).count()
    finally:
        db.close()
    if active and worker.spawn_worker(Path(settings.music_video_dir).resolve()):
        logger.info("Music-video worker restarted for %d queued job(s)", active)


app = FastAPI(title="Audiplex", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router.router)
app.include_router(library.router)
app.include_router(streaming.router)
app.include_router(progress.router)
app.include_router(music.router)
app.include_router(playback.router)
app.include_router(dj_voice.router)
app.include_router(dj_learn_router.router)  # #4057
app.include_router(music_video.router)  # #6172
app.include_router(app_router.router)
app.include_router(web.router)  # #2806 browser UI at /web


@app.get("/api/health")
def health():
    return {"status": "ok"}
