"""#5682: serve a copy of a music file cut at tracks.end_trim_seconds. The source is never edited."""
import glob
import os
import subprocess
import tempfile


def trimmed_path(
    track_id: int,
    src_path: str,
    end_seconds: float | None,
    cache_dir: str | None = None,
    ffmpeg: str = "ffmpeg",
    timeout: float = 120,
) -> str:
    tmp = None
    try:
        if end_seconds is None or end_seconds <= 0 or not os.path.exists(src_path):
            return src_path

        mtime_ns = os.stat(src_path).st_mtime_ns
        ext = os.path.splitext(src_path)[1]
        if cache_dir is None:
            cache_dir = os.path.join(tempfile.gettempdir(), "audiplex_trim")
        os.makedirs(cache_dir, exist_ok=True)

        name = f"{track_id}-{int(mtime_ns)}-{int(round(end_seconds * 1000))}{ext}"
        cache = os.path.join(cache_dir, name)
        if os.path.exists(cache) and os.path.getsize(cache) > 0:
            return cache

        tmp = os.path.splitext(cache)[0] + f".part{os.getpid()}" + ext
        argv = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-i", src_path, "-t", f"{end_seconds:.3f}",
            "-c", "copy", "-map_metadata", "0", tmp,
        ]
        result = subprocess.run(argv, capture_output=True, timeout=timeout)
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg exited with status {result.returncode}")
        if os.path.getsize(tmp) == 0:
            raise RuntimeError("ffmpeg produced empty output")

        os.replace(tmp, cache)
        for stale in glob.glob(os.path.join(cache_dir, f"{track_id}-*")):
            if stale != cache and ".part" not in os.path.basename(stale):
                os.remove(stale)
        return cache
    except Exception as exc:
        if tmp is not None:
            try:
                os.remove(tmp)
            except OSError:
                pass
        warning = " ".join(str(exc).splitlines())
        try:
            print(f"[trimmed] track {track_id}: {warning}", flush=True)
        except OSError:
            pass
        return src_path
