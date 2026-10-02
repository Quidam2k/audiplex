"""Music-video render worker (#6172). Runs as its own process, one job at a time.

    python -m audiplex.music_video.worker

The server spawns it when a job is queued (spawn_worker) and it exits when the
queue is empty, so a render that takes hours never lives inside uvicorn. A pid
lock keeps it to a single instance. Every finished clip stays on disk, so a
crash or restart resumes at the next clip.

Rendering goes through comfy_workflows' agent_gen library (the owning project's
entry point: `run("h3", params)`), MiniMax H3 image-to-video, one clip per image.
A clip marked "sing" also gets its slice of the song as H3's `soundtrack` guide,
so the singer in the picture lip syncs to the lyrics; the others stay plain i2v.
Every image is centre-cropped (never stretched) to the job's aspect ratio first.

GPU manners (Pantheon rules, comfy_workflows README):
  - never starts ComfyUI; waits until one answers on 8188 / 8000 / 8189
  - waits while Solace is held for Todd (gpu_modes solace=todd)
  - waits for an idle Comfy queue before every clip; never cancels or reorders
  - Solace only. Athena's GPU belongs to speech (#1179).
"""

import json
import logging
import os
import random
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

logger = logging.getLogger("audiplex.music_video")

COMFY_WORKFLOWS = Path(os.environ.get("AUDIPLEX_COMFY_WORKFLOWS", r"Q:\Development\comfy_workflows"))
COMFY_OUTPUT = Path(os.environ.get("AUDIPLEX_COMFY_OUTPUT", r"Q:\ComfyUI_Desktop\output"))
PANTHEON_SRC = Path(os.environ.get("AUDIPLEX_PANTHEON_SRC", r"Q:\Pantheon\src"))
COMFY_HOST = "127.0.0.1"
COMFY_PORTS = (8188, 8000, 8189)
WAIT_POLL = 30          # seconds between GPU-free checks
CLIP_TIMEOUT = 3600     # one clip; final quality is ~200 s, so this is a stall guard
ACTIVE = ("queued", "analyzing", "rendering", "stitching")

DEFAULT_MOTION = (
    "Cinematic music video shot. Natural, fluid motion with gentle camera movement; "
    "the scene comes alive."
)
# #6867: Todd can edit the whole prompt; {direction} is where his Direction text goes.
DEFAULT_TEMPLATE = "{direction}. " + DEFAULT_MOTION
_LORA = re.compile(r"<lora:[^>]*>", re.I)
VIDEO_EXT = {".mp4", ".webm", ".mov", ".mkv"}


# ---- prompt -----------------------------------------------------------------

def _clean(text: str) -> str:
    return " ".join(_LORA.sub("", text or "").split())


def lora_tags(*texts: str | None) -> list[str]:
    """Every <lora:name:weight> tag in the texts, in order, without duplicates."""
    out: list[str] = []
    for t in texts:
        for tag in _LORA.findall(t or ""):
            if tag not in out:
                out.append(tag)
    return out


def build_prompt(direction: str, template: str | None = None, loras: list[str] | None = None) -> str:
    """Fill Todd's prompt template (default: Direction, then the motion line) with
    his Direction text. An empty Direction drops "{direction}." cleanly.

    <lora:name:weight> tags (standard syntax) are lifted out of the text and put
    back at the end, plus any in `loras` (the job-wide ones, so a clip with its
    own direction still gets them). agent_gen's H3 loads them via LoraTagLoader."""
    tags = lora_tags(*(loras or []), direction, template)
    tpl = _clean(template) or DEFAULT_TEMPLATE
    d = _clean(direction).rstrip(". ")
    if "{direction}" not in tpl:
        text = f"{d}. {tpl}" if d else tpl
    elif d:
        text = tpl.replace("{direction}", d)
    else:
        text = re.sub(r"\{direction\}[.,;:]?\s*", "", tpl).strip() or DEFAULT_MOTION
    return " ".join([text, *tags])


# ---- frames / audio ---------------------------------------------------------

def crop_to_aspect(im, aspect: float):
    """Centre-crop a PIL image to width:height = aspect, never stretching."""
    w, h = im.size
    if w / h > aspect:
        nw = max(1, round(h * aspect))
        x = (w - nw) // 2
        return im.crop((x, 0, x + nw, h))
    nh = max(1, round(w / aspect))
    y = (h - nh) // 2
    return im.crop((0, y, w, y + nh))


def prepare_frame(src: str, w: int, h: int, dest: Path) -> Path:
    """The first frame H3 gets: the image cropped to w:h and resized to exactly w x h."""
    from PIL import Image

    dest.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(src) as im:
        im = crop_to_aspect(im.convert("RGB"), w / h).resize((w, h), Image.LANCZOS)
        im.save(dest, "PNG")
    return dest


def slice_song(song: str, start: float, end: float, dest: Path) -> Path:
    """The clip's slice of the song (full mix) as 48 kHz stereo WAV, for H3's soundtrack guide."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    _ffmpeg("-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", song, "-vn", "-ac", "2", "-ar", "48000", str(dest))
    return dest


def _clips(image_paths: str) -> list[dict]:
    """Normalise image_paths: plain strings (pre lip-sync jobs) -> clip dicts."""
    out = []
    for c in json.loads(image_paths):
        if isinstance(c, str):
            c = {"path": c}
        out.append({"path": c["path"], "sing": bool(c.get("sing")), "prompt": c.get("prompt") or ""})
    return out


# ---- ComfyUI / GPU ----------------------------------------------------------

def probe_comfy_port(ports=COMFY_PORTS, host=COMFY_HOST) -> int | None:
    for port in ports:
        try:
            with urllib.request.urlopen(f"http://{host}:{port}/system_stats", timeout=2) as r:
                if r.status == 200 and "system" in json.load(r):
                    return port
        except Exception:
            continue
    return None


def solace_held_for_todd() -> bool:
    """gpu_modes is Pantheon's single source for "keep off Solace's 4090"."""
    try:
        if str(PANTHEON_SRC) not in sys.path:
            sys.path.append(str(PANTHEON_SRC))
        import gpu_modes

        return gpu_modes.current("solace").get("mode") == "todd"
    except Exception as exc:  # unreadable store: Comfy's own queue check still applies
        logger.warning("gpu_modes unreadable (%s); not treating Solace as held", exc)
        return False


def _queue_busy(port: int) -> bool:
    with urllib.request.urlopen(f"http://{COMFY_HOST}:{port}/queue", timeout=30) as r:
        q = json.load(r)
    return bool(q.get("queue_running") or q.get("queue_pending"))


def gpu_wait_reason(port: int | None) -> str | None:
    """None when it's OK to submit a clip right now, else why we're waiting."""
    if port is None:
        return "Waiting for ComfyUI to be started on Solace"
    if solace_held_for_todd():
        return "Waiting: Solace's GPU is held for Todd"
    try:
        if _queue_busy(port):
            return "Waiting: ComfyUI is busy with another job"
    except Exception as exc:
        logger.warning("ComfyUI queue check on :%s failed: %s", port, exc)
        return "Waiting for ComfyUI to answer"
    return None


def _agent_gen_run():
    if str(COMFY_WORKFLOWS) not in sys.path:
        sys.path.append(str(COMFY_WORKFLOWS))
    from agent_gen.run import run

    return run


def _video_output(outputs) -> Path:
    for o in outputs:
        name = o.get("filename") or ""
        if Path(name).suffix.lower() in VIDEO_EXT:
            return COMFY_OUTPUT / (o.get("subfolder") or "") / name
    raise RuntimeError(f"ComfyUI returned no video ({outputs!r:.300})")


def render_clip(*, image: str, seconds: float, quality: str, prompt: str,
                port: int, prefix: str, dest: Path, run=None, soundtrack: Path | None = None,
                aspect: str = "16:9") -> Path:
    from audiplex.music_video.planner import QUALITY, frame_size, h3_length

    w, h = frame_size(quality, aspect)
    params = {
        "prompt": prompt,
        "width": w,
        "height": h,
        "steps": QUALITY[quality]["steps"],
        "length": h3_length(seconds),
        "seed": random.randint(0, 2**31 - 1),  # a fresh seed per clip (batch-seed-reuse lesson)
        "first_frame": image,
        "filename_prefix": prefix,
    }
    if soundtrack is not None:  # H3 flips to guided mode; first_frame is still honoured
        params["soundtrack"] = str(soundtrack)
    run = run or _agent_gen_run()
    result = run("h3", params, host=COMFY_HOST, port=port, timeout=CLIP_TIMEOUT, wait_for_idle=True)
    src = _video_output(result["outputs"])
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part" + src.suffix)
    shutil.copyfile(src, tmp)
    tmp.replace(dest)
    return dest


# ---- ffmpeg -----------------------------------------------------------------

def _ffmpeg(*args):
    r = subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {r.stderr.strip()[-400:]}")


def stitch(clips: list[Path], segments: list[tuple[float, float]], song: str, out: Path, workdir: Path) -> Path:
    """Trim each clip to its slot (dropping H3's own audio), join, lay the song over.

    The song goes on in one pass at the end (-map 0:v -map 1:a), the fix the
    comfy_workflows music-video runs settled on; per-clip audio never survives.
    """
    from audiplex.music_video.planner import FPS

    workdir.mkdir(parents=True, exist_ok=True)
    trimmed = []
    for i, (clip, (start, end)) in enumerate(zip(clips, segments)):
        t = workdir / f"trim_{i:03d}.mp4"
        # Frame counts come from cumulative song time, so rounding never drifts
        # the cuts off the beat over a long song. tpad guards a clip that comes
        # back a frame short of its slot.
        frames = round(end * FPS) - round(start * FPS)
        _ffmpeg("-i", str(clip), "-an", "-vf", f"fps={FPS},tpad=stop_mode=clone:stop_duration=1",
                "-frames:v", str(frames), "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-pix_fmt", "yuv420p", str(t))
        trimmed.append(t)
    listing = workdir / "concat.txt"
    listing.write_text("\n".join(f"file '{p.as_posix()}'" for p in trimmed), encoding="utf-8")
    silent = workdir / "video_only.mp4"
    _ffmpeg("-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(silent))
    out.parent.mkdir(parents=True, exist_ok=True)
    _ffmpeg("-i", str(silent), "-i", song, "-map", "0:v", "-map", "1:a", "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", str(out))
    return out


# ---- jobs -------------------------------------------------------------------

class Cancelled(Exception):
    pass


def job_dir(base: Path, job_id: int) -> Path:
    return base / "jobs" / f"job_{job_id:05d}"


def process_job(db, job, base: Path, *, run=None, sleep=time.sleep) -> None:
    """Analyze, plan, render every clip, stitch. Resumes from finished clips."""
    from audiplex.models import Track
    from audiplex.music_video import analysis, planner

    def save(**fields):
        db.refresh(job)
        if job.status == "cancelled":
            raise Cancelled()
        for k, v in fields.items():
            setattr(job, k, v)
        db.commit()

    track = db.get(Track, job.track_id)
    if track is None:
        raise RuntimeError(f"track {job.track_id} no longer exists")
    images = _clips(job.image_paths)
    jdir = job_dir(base, job.id)

    if job.plan_json:
        plan = json.loads(job.plan_json)
    else:
        save(status="analyzing", detail="Finding the beats and where the vocals are")
        a = analysis.analyze(track.file_path, base / "analysis")
        segs = planner.plan_segments(a["duration"], a["beats"], a["vocal_spans"], len(images))
        plan = {
            "duration": a["duration"], "tempo": a["tempo"],
            "segments": [{"start": s.start, "end": s.end, "forced": s.forced} for s in segs],
        }
        save(plan_json=json.dumps(plan), clips_total=len(segs))

    aspect = job.aspect or "16:9"
    w, h = planner.frame_size(job.quality, aspect)
    job_loras = lora_tags(job.direction)  # apply to every clip, even ones with their own direction
    clips = []
    for i, (seg, clip) in enumerate(zip(plan["segments"], images)):
        dest = jdir / "clips" / f"clip_{i:03d}.mp4"
        clips.append(dest)
        if dest.exists():
            continue
        prompt = build_prompt(clip["prompt"] or job.direction, job.prompt_template, job_loras)
        frame = prepare_frame(clip["path"], w, h, jdir / "frames" / f"frame_{i:03d}.png")
        soundtrack = None
        if clip["sing"]:
            soundtrack = slice_song(track.file_path, seg["start"], seg["end"], jdir / "audio" / f"clip_{i:03d}.wav")
        while True:
            port = probe_comfy_port()
            reason = gpu_wait_reason(port)
            if reason is None:
                break
            save(status="rendering", detail=reason)
            sleep(WAIT_POLL)
        logger.info("job %s clip %d/%d on Comfy :%s", job.id, i + 1, len(images), port)
        save(status="rendering", detail=f"Rendering clip {i + 1} of {len(images)} (ComfyUI :{port})",
             clips_done=i)
        render_clip(image=str(frame), seconds=seg["end"] - seg["start"], quality=job.quality,
                    prompt=prompt, port=port, prefix=f"audiplex_mv/job_{job.id:05d}/clip_{i:03d}",
                    dest=dest, run=run, soundtrack=soundtrack, aspect=aspect)

    save(status="stitching", detail="Joining the clips and laying the song over them",
         clips_done=len(clips))
    out = base / "videos" / f"music_video_{job.id:05d}.mp4"
    stitch(clips, [(s["start"], s["end"]) for s in plan["segments"]], track.file_path, out, jdir / "stitch")
    save(status="done", detail="Ready", output_path=str(out.resolve()))


# ---- process management -----------------------------------------------------

def _lock_path(base: Path) -> Path:
    return base / "worker.pid"


def worker_alive(base: Path) -> bool:
    import psutil

    try:
        pid = int(_lock_path(base).read_text().strip())
    except (OSError, ValueError):
        return False
    try:
        return "audiplex.music_video.worker" in " ".join(psutil.Process(pid).cmdline())
    except (psutil.Error, OSError):
        return False


def spawn_worker(base: Path) -> bool:
    """Start the worker unless one is already running. Returns True if started."""
    if worker_alive(base):
        return False
    base.mkdir(parents=True, exist_ok=True)
    server_dir = Path(__file__).resolve().parents[2]
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    log = open(base / "worker.log", "ab")
    subprocess.Popen([sys.executable, "-m", "audiplex.music_video.worker"], cwd=server_dir,
                     stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                     creationflags=flags, close_fds=True)
    return True


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from audiplex import database
    from audiplex.config import get_settings
    from audiplex.models import MusicVideoJob

    settings = get_settings()
    base = Path(settings.music_video_dir).resolve()
    base.mkdir(parents=True, exist_ok=True)
    if worker_alive(base):
        logger.info("another music-video worker is running; exiting")
        return 0
    _lock_path(base).write_text(str(os.getpid()))

    database.init_db(settings.database_url)
    db = database._SessionLocal()
    try:
        while True:
            job = (db.query(MusicVideoJob).filter(MusicVideoJob.status.in_(ACTIVE))
                   .order_by(MusicVideoJob.id).first())
            if job is None:
                logger.info("music-video queue empty; worker exiting")
                return 0
            try:
                process_job(db, job, base)
            except Cancelled:
                logger.info("job %s cancelled", job.id)
            except Exception as exc:
                logger.exception("job %s failed", job.id)
                db.rollback()
                job.status, job.detail = "failed", str(exc)[:500]
                db.commit()
    finally:
        db.close()
        try:
            if _lock_path(base).read_text().strip() == str(os.getpid()):
                _lock_path(base).unlink()
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
