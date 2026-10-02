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
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from audiplex.auth import get_admin_user
from audiplex.config import get_settings
from audiplex.database import get_db
from audiplex.models import MusicVideoJob, Track
from audiplex.music_video import analysis, planner, worker

router = APIRouter(prefix="/api/music-video", tags=["music-video"])

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
VIDEO_URL_TTL = 6 * 3600
MAX_DIRECTION = 1000
MAX_TEMPLATE = 2000


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


def _aspect(a: str) -> str:
    if a not in planner.ASPECTS:
        raise HTTPException(400, f"aspect must be one of {list(planner.ASPECTS)}")
    return a


def _job_out(j: MusicVideoJob) -> dict:
    return {
        "id": j.id, "track_id": j.track_id, "quality": j.quality,
        "aspect": j.aspect or "16:9", "status": j.status,
        "detail": j.detail, "clips_total": j.clips_total, "clips_done": j.clips_done,
        "image_folder": j.image_folder, "direction": j.direction,
        "prompt_template": j.prompt_template,
        "sing_count": sum(1 for c in worker._clips(j.image_paths) if c["sing"]),
        "has_video": bool(j.status == "done" and j.output_path),
        "created_at": j.created_at.isoformat() if j.created_at else None,
    }


# ---- estimate / plan ---------------------------------------------------------

@router.get("/estimate/{track_id}")
def estimate(track_id: int, quality: str = "draft", db: Session = Depends(get_db),
             _user=Depends(get_admin_user)):
    """The song plus what Todd used last time. The image count comes from /plan."""
    _quality(quality)
    t = _track(db, track_id)
    last = db.query(MusicVideoJob).order_by(MusicVideoJob.id.desc()).first()
    return {
        "track_id": t.id, "title": t.title, "duration": t.duration_seconds, "quality": quality,
        "clip_min_seconds": planner.VAR_MIN, "clip_max_seconds": planner.VAR_MAX,
        # remembered from his last job (never a default corpus path)
        "last_folder": last.image_folder if last else None,
        "last_direction": last.direction if last else "",
        "last_prompt_template": (last.prompt_template if last else None) or worker.DEFAULT_TEMPLATE,
        "default_prompt_template": worker.DEFAULT_TEMPLATE,
        "aspects": list(planner.ASPECTS),
        "last_aspect": (last.aspect if last else None) or "16:9",
    }


# Song analysis (demucs on CPU, ~1-2 min per song) runs before picking (#6867),
# because the image count depends on where the lyrics fall. One thread per file;
# the result lands in analysis's on-disk cache.
_analysis_lock = threading.Lock()
_analysis_running: set[str] = set()
_analysis_failed: dict[str, str] = {}


def _analyze_bg(path: str, cache_dir: Path) -> None:
    try:
        analysis.analyze(path, cache_dir)
    except Exception as exc:
        with _analysis_lock:
            _analysis_failed[path] = str(exc)[:300]
    finally:
        with _analysis_lock:
            _analysis_running.discard(path)


def _start_analysis(path: str, cache_dir: Path) -> None:
    threading.Thread(target=_analyze_bg, args=(path, cache_dir), daemon=True).start()


def _plan_for(t: Track, quality: str, start: bool = True) -> dict:
    cache_dir = _base() / "analysis"
    try:
        a = analysis.cached(t.file_path, cache_dir)
    except OSError:
        raise HTTPException(404, "The song's file is missing")
    if a is not None:
        segs = planner.plan_variable(a["duration"], a["beats"], a["vocal_spans"])
        return {"status": "ready", "duration": a["duration"], "tempo": a["tempo"],
                **planner.plan_dict(segs, quality, a["vocal_spans"])}
    with _analysis_lock:
        if t.file_path in _analysis_failed:
            return {"status": "failed", "detail": _analysis_failed[t.file_path]}
        if start and t.file_path not in _analysis_running:
            _analysis_running.add(t.file_path)
            _start_analysis(t.file_path, cache_dir)
    # demucs on CPU runs at roughly 1.2x real time on Solace
    return {"status": "analyzing", "est_analysis_seconds": int(30 + 1.2 * (t.duration_seconds or 0))}


@router.get("/plan/{track_id}")
def plan(track_id: int, quality: str = "draft", retry: bool = False,
         db: Session = Depends(get_db), _user=Depends(get_admin_user)):
    """Analyze the song (in the background) and plan its 5-15 s clips.

    status analyzing -> poll again; ready -> n_images, per-clip range and the
    render estimate; failed -> detail (pass retry=1 to try again)."""
    _quality(quality)
    t = _track(db, track_id)
    if retry:
        with _analysis_lock:
            _analysis_failed.pop(t.file_path, None)
    return {"track_id": t.id, "quality": quality, **_plan_for(t, quality)}


# ---- folder browsing --------------------------------------------------------

def _read_size(path: str) -> tuple:
    """Pixel size from the image header (PIL doesn't decode the pixels here)."""
    from PIL import Image

    try:
        with Image.open(path) as im:
            return im.size
    except Exception:
        return (None, None)


TAG_THRESHOLD = 0.35  # batch_wd14_tag.py's default cutoff
TAG_SIDECAR = ".wd14cache.json"  # comfy_workflows/batch_wd14_tag.py writes <image>.wd14cache.json


def _read_tags(path: str) -> list | None:
    """WD14 tags at or above TAG_THRESHOLD from the image's sidecar (it holds all ~11k scores)."""
    try:
        with open(path, encoding="utf-8") as fh:
            conf = json.load(fh)["tag_confidences"]
        return sorted(t for t, c in conf.items() if c >= TAG_THRESHOLD)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


# Image sizes and tags, cached on disk per folder by (mtime, bytes) of the image and
# the mtime of its tag sidecar: reading 13k headers (and 440 KB sidecars) off Todd's
# external USB disk cold takes minutes; a stat comes free with the listing.
# Entry: [mtime_ns, bytes, width, height, sidecar_mtime_ns | None, tags | None].
_sizes_lock = threading.Lock()


def _stat_key(e) -> list | None:
    try:
        st = e.stat()
        return [st.st_mtime_ns, st.st_size]
    except OSError:  # vanished or locked since the listing
        return None


def _image_info(folder: Path, entries: list, sidecars: dict) -> dict:
    cache_file = _base() / "image_info.json"
    with _sizes_lock:
        try:
            cache = json.loads(cache_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cache = {}
        old = cache.get(str(folder), {})
        out, need_size, need_tags = {}, [], []
        for e in entries:
            key = _stat_key(e)
            side = sidecars.get(e.name)
            skey = (_stat_key(side) or [None])[0] if side is not None else None
            hit = old.get(e.name) or []
            if key and hit[:2] == key:
                row = hit[:4]
            else:
                row = (key or [0, 0]) + [None, None]
                if key:
                    need_size.append((e.name, e.path))
            if len(hit) == 6 and hit[4] == skey and hit[:2] == key:
                row += hit[4:]
            else:
                row += [skey, None]
                if skey is not None:
                    need_tags.append((e.name, side.path))
            out[e.name] = row
        if need_size or need_tags:
            with ThreadPoolExecutor(16) as pool:
                for (name, _), wh in zip(need_size, pool.map(lambda m: _read_size(m[1]), need_size)):
                    out[name][2:4] = list(wh)
                for (name, _), tags in zip(need_tags, pool.map(lambda m: _read_tags(m[1]), need_tags)):
                    out[name][5] = tags
        if need_size or need_tags or out.keys() != old.keys():
            cache[str(folder)] = out
            try:
                _base().mkdir(parents=True, exist_ok=True)
                tmp = cache_file.with_suffix(".tmp")
                tmp.write_text(json.dumps(cache), encoding="utf-8")
                tmp.replace(cache_file)
            except OSError:
                pass
    return {name: {"width": v[2], "height": v[3], "tags": v[5]} for name, v in out.items()}


@router.get("/images")
def list_images(folder: str, _user=Depends(get_admin_user)):
    f = _folder(folder)
    files, subfolders, sidecars = [], [], {}
    try:
        entries = sorted(os.scandir(f), key=lambda e: e.name.lower())
    except OSError as exc:
        raise HTTPException(403, f"Can't read folder: {exc.strerror}")
    for e in entries:
        try:
            if e.is_dir():
                subfolders.append(e.name)
            elif Path(e.name).suffix.lower() in IMAGE_EXT:
                files.append(e)
            elif e.name.endswith(TAG_SIDECAR):
                sidecars[e.name[:-len(TAG_SIDECAR)]] = e
        except OSError:
            continue
    info = _image_info(f, files, sidecars)
    # tags go out as indices into one vocabulary (most common first), not 8k repeated strings
    counts: dict[str, int] = {}
    for v in info.values():
        for t in v["tags"] or ():
            counts[t] = counts.get(t, 0) + 1
    vocab = sorted(counts, key=lambda t: (-counts[t], t))
    index = {t: i for i, t in enumerate(vocab)}
    images = []
    for e in files:
        v = info[e.name]
        tags = None if v["tags"] is None else [index[t] for t in v["tags"]]
        images.append({"name": e.name, "width": v["width"], "height": v["height"], "tags": tags})
    parent = str(f.parent) if f.parent != f else None
    return {"folder": str(f), "parent": parent, "subfolders": subfolders, "images": images,
            "tags": vocab, "tag_counts": [counts[t] for t in vocab]}


@router.get("/thumb")
def thumb(folder: str, name: str, aspect: str = "16:9", _user=Depends(get_admin_user)):
    from PIL import Image

    aw, ah = planner.ASPECTS[_aspect(aspect)]
    p = _image_in(_folder(folder), name)
    try:
        with Image.open(p) as im:
            # the same centre crop the render gets, so the thumb is the frame
            im = worker.crop_to_aspect(im.convert("RGB"), aw / ah)
            im.thumbnail((480, 480))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=80)
    except OSError:
        raise HTTPException(415, "Not a readable image")
    return Response(buf.getvalue(), media_type="image/jpeg",
                    headers={"Cache-Control": "private, max-age=3600"})


# ---- jobs -------------------------------------------------------------------

class ImageIn(BaseModel):
    """One picked image: `sing` = lip sync this clip to the song; `prompt` = Direction
    for this clip only ("" = the job's)."""
    name: str
    sing: bool = False
    prompt: str = ""


class JobIn(BaseModel):
    track_id: int
    quality: str = "draft"
    aspect: str = "16:9"
    folder: str
    images: list[ImageIn] = Field(min_length=1)
    direction: str = ""
    prompt_template: str = ""


@router.post("/jobs", status_code=201)
def create_job(body: JobIn, db: Session = Depends(get_db), _user=Depends(get_admin_user)):
    _quality(body.quality)
    _aspect(body.aspect)
    t = _track(db, body.track_id)
    f = _folder(body.folder)
    clips = []
    for im in body.images:
        if len(im.prompt) > MAX_DIRECTION:
            raise HTTPException(400, f"A clip's direction is limited to {MAX_DIRECTION} characters")
        clips.append({"path": str(_image_in(f, im.name)), "sing": im.sing, "prompt": im.prompt.strip()})
    if len(body.direction) > MAX_DIRECTION:
        raise HTTPException(400, f"Direction is limited to {MAX_DIRECTION} characters")
    if len(body.prompt_template) > MAX_TEMPLATE:
        raise HTTPException(400, f"Video prompt is limited to {MAX_TEMPLATE} characters")
    p = _plan_for(t, body.quality, start=False)
    if p["status"] != "ready":
        raise HTTPException(409, "The song hasn't been analyzed yet")
    need = p["n_images"]
    if len(clips) != need:
        raise HTTPException(400, f"This song needs exactly {need} images; got {len(clips)}")
    tpl = body.prompt_template.strip()
    plan_json = json.dumps({"duration": p["duration"], "tempo": p["tempo"], "segments": p["segments"]})
    job = MusicVideoJob(track_id=t.id, quality=body.quality, aspect=body.aspect,
                        image_folder=str(f),
                        image_paths=json.dumps(clips), direction=body.direction.strip(),
                        prompt_template=None if tpl in ("", worker.DEFAULT_TEMPLATE) else tpl,
                        plan_json=plan_json, status="queued", detail="Queued", clips_total=need)
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


@router.post("/jobs/{job_id}/rerender", status_code=201)
def rerender_job(job_id: int, quality: str = "final", db: Session = Depends(get_db),
                 _user=Depends(get_admin_user)):
    """The same video again at another quality: same song, images, Sings ticks,
    per-clip and overall directions, prompt, aspect and cut points. It queues
    behind whatever is rendering."""
    _quality(quality)
    j = _job(db, job_id)
    missing = [c["path"] for c in worker._clips(j.image_paths) if not Path(c["path"]).is_file()]
    if missing:
        raise HTTPException(409, f"{len(missing)} of its images are gone, e.g. {Path(missing[0]).name}")
    new = MusicVideoJob(track_id=j.track_id, quality=quality, aspect=j.aspect or "16:9",
                        image_folder=j.image_folder, image_paths=j.image_paths, direction=j.direction,
                        prompt_template=j.prompt_template, plan_json=j.plan_json, status="queued",
                        detail=f"Queued (re-render of #{j.id})", clips_total=j.clips_total)
    db.add(new)
    db.commit()
    db.refresh(new)
    worker.spawn_worker(_base())
    return _job_out(new)


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
