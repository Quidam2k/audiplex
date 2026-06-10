"""Audiplex — self-hosted audiobook server."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from audiplex.config import get_settings
from audiplex.database import get_db, init_db
from audiplex.routers import app as app_router
from audiplex.routers import auth_router, library, music, progress, streaming
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

    yield


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
app.include_router(app_router.router)


@app.get("/api/health")
def health():
    return {"status": "ok"}
