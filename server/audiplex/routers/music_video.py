"""Music video API (#6172): pick a song, pick N images from a folder, render.

BOUNDARY (Todd msg 39562, #3319): images come ONLY from the folder Todd types
into the UI. There is no default folder and no corpus path anywhere in this
code; every image name is a bare filename re-resolved inside the chosen folder,
so nothing outside it can be listed, thumbnailed or rendered.

<img> and <video> can't send the Bearer header, so the page fetches thumbnails
as blobs, and the finished video gets a short-lived signed URL (video-url).
"""

import hashlib
import hmac
import io
import json
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from audiplex.auth import get_admin_user
from audiplex.config import get_settings
from audiplex.database import get_db
from audiplex.models import MusicVideoJob, Track
from audiplex.music_video import planner, worker

router = APIRouter(prefix="/api/music-video", tags=["music-video"])

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
VIDEO_URL_TTL = 6 * 3600
MAX_DIRECTION = 1000


def _base() -> Path:
    return Path(get_settings().music_video_dir).resolve()


def _folder(path: str) -> Path:
    """The folder Todd chose: absolute, existing, a directory."""
    p = Path(path or "")
    if not path or not p.is_absolute():
        raise HTTPException(400, "Enter the full path of a folder")
    if not p.is_dir():
        raise HTTPException(404, "Folder not found")
    return p.resolve()


def _image_in(folder: Path, name: str) -> Path:
    """A bare image filename, resolved strictly inside `folder`."""
    if not name or name != Path(name).name or name in (".", ".."):
        raise HTTPException(400, "Bad image name")
    p = (folder / name).resolve()
    if p.parent != folder or p.suffix.lower() not in IMAGE_EXT or not p.is_file():
        raise HTTPException(404, f"Image not found: {name}")
    return p


def _track(db: Session, track_id: int) -> Track:
    t = db.get(Track, track_id)
    if t is None:
        raise HTTPException(404, "Track not found")
    return t


def _quality(q: str) -> dict:
    if q not in planner.QUALITY:
        raise HTTPException(400, f"quality must be one of {sorted(planner.QUALITY)}")
    return planner.QUALITY[q]


def _job_out(j: MusicVideoJob) -> dict:
    return {
        "id": j.id, "track_id": j.track_id, "quality": j.quality, "status": j.status,
        "detail": j.detail, "clips_total": j.clips_total, "clips_done": j.clips_done,
        "image_folder": j.image_folder, "direction": j.direction,
        "has_video": bool(j.status == "done" and j.output_path),
        "created_at": j.created_at.isoformat() if j.created_at else None,
    }


# ---- estimate ---------------------------------------------------------------

@router.get("/estimate/{track_id}")
def estimate(track_id: int, quality: str = "draft", db: Session = Depends(get_db),
             _user=Depends(get_admin_user)):
    q = _quality(quality)
    t = _track(db, track_id)
    n = planner.images_needed(t.duration_seconds, q["clip_seconds"])
    last = db.query(MusicVideoJob).order_by(MusicVideoJob.id.desc()).first()
    return {
        "track_id": t.id, "title": t.title, "duration": t.duration_seconds,
        "quality": quality, "clip_seconds": q["clip_seconds"], "n_images": n,
        "est_render_seconds": planner.estimate_render_seconds(n, quality),
        # remembered from his last job (never a default corpus path)
        "last_folder": last.image_folder if last else None,
        "last_direction": last.direction if last else "",
    }


# ---- folder browsing --------------------------------------------------------

@router.get("/images")
def list_images(folder: str, _user=Depends(get_admin_user)):
    f = _folder(folder)
    images, subfolders = [], []
    try:
        entries = sorted(f.iterdir(), key=lambda p: p.name.lower())
    except OSError as exc:
        raise HTTPException(403, f"Can't read folder: {exc.strerror}")
    for p in entries:
        try:
            if p.is_dir():
                subfolders.append(p.name)
            elif p.suffix.lower() in IMAGE_EXT:
                images.append(p.name)
        except OSError:
            continue
    parent = str(f.parent) if f.parent != f else None
    return {"folder": str(f), "parent": parent, "subfolders": subfolders, "images": images}


@router.get("/thumb")
def thumb(folder: str, name: str, _user=Depends(get_admin_user)):
    from PIL import Image

    p = _image_in(_folder(folder), name)
    try:
        with Image.open(p) as im:
            im = im.convert("RGB")
            im.thumbnail((320, 320))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=80)
    except OSError:
        raise HTTPException(415, "Not a readable image")
    return Response(buf.getvalue(), media_type="image/jpeg",
                    headers={"Cache-Control": "private, max-age=3600"})


# ---- jobs -------------------------------------------------------------------

class JobIn(BaseModel):
    track_id: int
    quality: str = "draft"
    folder: str
    images: list[str] = Field(min_length=1)
    direction: str = ""


@router.post("/jobs", status_code=201)
def create_job(body: JobIn, db: Session = Depends(get_db), _user=Depends(get_admin_user)):
    q = _quality(body.quality)
    t = _track(db, body.track_id)
    f = _folder(body.folder)
    paths = [str(_image_in(f, n)) for n in body.images]
    need = planner.images_needed(t.duration_seconds, q["clip_seconds"])
    if len(paths) != need:
        raise HTTPException(400, f"This song needs exactly {need} images; got {len(paths)}")
    if len(body.direction) > MAX_DIRECTION:
        raise HTTPException(400, f"Direction is limited to {MAX_DIRECTION} characters")
    job = MusicVideoJob(track_id=t.id, quality=body.quality, image_folder=str(f),
                        image_paths=json.dumps(paths), direction=body.direction.strip(),
                        status="queued", detail="Queued", clips_total=need)
    db.add(job)
    db.commit()
    db.refresh(job)
    worker.spawn_worker(_base())
    return _job_out(job)


@router.get("/jobs")
def list_jobs(db: Session = Depends(get_db), _user=Depends(get_admin_user)):
    jobs = db.query(MusicVideoJob).order_by(MusicVideoJob.id.desc()).limit(50).all()
    titles = {t.id: t.title for t in db.query(Track).filter(Track.id.in_({j.track_id for j in jobs}))}
    return [{**_job_out(j), "title": titles.get(j.track_id, "")} for j in jobs]


def _job(db: Session, job_id: int) -> MusicVideoJob:
    j = db.get(MusicVideoJob, job_id)
    if j is None:
        raise HTTPException(404, "Job not found")
    return j


@router.get("/jobs/{job_id}")
def get_job(job_id: int, db: Session = Depends(get_db), _user=Depends(get_admin_user)):
    return _job_out(_job(db, job_id))


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: int, db: Session = Depends(get_db), _user=Depends(get_admin_user)):
    j = _job(db, job_id)
    if j.status in worker.ACTIVE:
        j.status, j.detail = "cancelled", "Cancelled (the clip in progress finishes first)"
        db.commit()
    return _job_out(j)


@router.post("/jobs/{job_id}/retry")
def retry_job(job_id: int, db: Session = Depends(get_db), _user=Depends(get_admin_user)):
    """Requeue a failed/cancelled job; finished clips are reused."""
    j = _job(db, job_id)
    if j.status in ("failed", "cancelled"):
        j.status, j.detail = "queued", "Queued (resuming)"
        db.commit()
        worker.spawn_worker(_base())
    return _job_out(j)


# ---- video (signed URL, so <video> can stream it with range requests) -------

def _sig(job_id: int, exp: int) -> str:
    key = get_settings().jwt_secret.encode()
    return hmac.new(key, f"music-video:{job_id}:{exp}".encode(), hashlib.sha256).hexdigest()


@router.get("/jobs/{job_id}/video-url")
def video_url(job_id: int, db: Session = Depends(get_db), _user=Depends(get_admin_user)):
    j = _job(db, job_id)
    if j.status != "done" or not j.output_path:
        raise HTTPException(409, "Video not ready")
    exp = int(time.time()) + VIDEO_URL_TTL
    return {"url": f"/api/music-video/jobs/{job_id}/video?exp={exp}&sig={_sig(job_id, exp)}",
            "path": j.output_path}


@router.get("/jobs/{job_id}/video")
def video(job_id: int, exp: int = Query(...), sig: str = Query(...), db: Session = Depends(get_db)):
    if exp < time.time() or not hmac.compare_digest(sig, _sig(job_id, exp)):
        raise HTTPException(403, "Link expired")
    j = _job(db, job_id)
    if j.status != "done" or not j.output_path or not Path(j.output_path).is_file():
        raise HTTPException(404, "Video not found")
    return FileResponse(j.output_path, media_type="video/mp4",
                        filename=f"music_video_{job_id:05d}.mp4", content_disposition_type="inline")
