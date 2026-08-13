"""DJ voice-break clips (item #431).

The agent writes on-air copy, synthesizes it to audio MCP-side, uploads the
clip here, then enqueues an `announce` command through the existing playback
bus so the device drops the break into its queue between songs.

Endpoints:
  POST /api/dj/clips           — agent uploads a synthesized clip
  GET  /api/dj/clips/{clip_id} — device fetches it (range-capable)

Deliberately NOT a database table: clips are ephemeral by nature (a break is
stale the moment the song after it ends), so they live on disk keyed by an
epoch-ms id and are pruned after CLIP_TTL_DAYS. That keeps #431 free of any
schema change — and the bus needs no change either, since
PlaybackCommand.type is free-form.
"""

import os
import time
from pathlib import Path

import mutagen
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile

from audiplex.auth import get_current_user
from audiplex.config import get_settings
from audiplex.models import User
from audiplex.schemas import DjClipCreated
from audiplex.utils.streaming import EXT_MIME, serve_file

router = APIRouter(prefix="/api/dj", tags=["dj"])

CLIP_TTL_DAYS = 7
ALLOWED_EXTS = {".wav", ".mp3", ".m4a", ".ogg", ".flac"}
MAX_CLIP_BYTES = 20 * 1024 * 1024  # a spoken break is seconds long; 20MB is generous


def _clip_dir() -> Path:
    d = Path(get_settings().dj_clip_dir)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _prune(directory: Path) -> int:
    """Delete clips older than CLIP_TTL_DAYS. Best-effort: a locked or
    already-deleted file must never fail the upload that triggered the prune."""
    cutoff = time.time() - CLIP_TTL_DAYS * 86400
    removed = 0
    for f in directory.iterdir():
        if not f.is_file() or f.suffix.lower() not in ALLOWED_EXTS:
            continue
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                removed += 1
        except OSError:
            pass
    return removed


def _find_clip(clip_id: int) -> Path | None:
    directory = _clip_dir()
    for ext in ALLOWED_EXTS:
        candidate = directory / f"{clip_id}{ext}"
        if candidate.is_file():
            return candidate
    return None


@router.post("/clips", response_model=DjClipCreated)
async def upload_clip(
    request: Request,
    file: UploadFile = File(...),
    title: str = Form("DJ break"),
    user: User = Depends(get_current_user),
):
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_EXTS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported clip type '{ext}'. Allowed: {sorted(ALLOWED_EXTS)}",
        )

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty clip upload")
    if len(data) > MAX_CLIP_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Clip too large ({len(data)} bytes, max {MAX_CLIP_BYTES})",
        )

    directory = _clip_dir()
    _prune(directory)

    # Epoch-ms id, nudged forward on the rare same-millisecond collision so a
    # clip can never overwrite one that's already queued for playback.
    clip_id = int(time.time() * 1000)
    while (directory / f"{clip_id}{ext}").exists():
        clip_id += 1

    path = directory / f"{clip_id}{ext}"
    path.write_bytes(data)

    duration: float | None = None
    try:
        probed = mutagen.File(str(path))
        if probed is not None and probed.info is not None:
            duration = float(probed.info.length)
    except Exception:
        duration = None  # a clip that won't probe still plays; don't fail the upload

    return DjClipCreated(
        clip_id=clip_id,
        url=f"/api/dj/clips/{clip_id}",
        duration_seconds=duration,
    )


@router.get("/clips/{clip_id}")
def get_clip(clip_id: int, request: Request, user: User = Depends(get_current_user)):
    path = _find_clip(clip_id)
    if path is None:
        raise HTTPException(status_code=404, detail=f"Clip {clip_id} not found")
    ext = os.path.splitext(path)[1].lower()
    return serve_file(str(path), EXT_MIME.get(ext, "application/octet-stream"), request)
