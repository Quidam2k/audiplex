"""Song analysis for music videos (#6172): duration, beat grid, vocal spans.

Vocals come from a Demucs two-stem split (htdemucs, run on CPU so it never
competes with ComfyUI or speech for a GPU); a span is wherever the vocal stem's
loudness sits within VOCAL_DB of its peak. Results are cached per file.
"""

import hashlib
import json
import logging
import subprocess
import sys
import tempfile
from pathlib import Path

logger = logging.getLogger("audiplex.music_video")

VOCAL_DB = 30.0       # louder than (peak - 30 dB) counts as singing
MERGE_GAP = 0.35      # breaths shorter than this don't split a vocal span
MIN_SPAN = 0.20       # ignore blips shorter than this


def vocal_spans_from_rms(rms_db, hop_seconds: float) -> list[list[float]]:
    """Turn a per-frame dB curve of the vocal stem into [start, end] spans."""
    if len(rms_db) == 0:
        return []
    floor = max(rms_db) - VOCAL_DB
    spans: list[list[float]] = []
    start = None
    for i, db in enumerate(rms_db):
        t = i * hop_seconds
        if db > floor and start is None:
            start = t
        elif db <= floor and start is not None:
            spans.append([start, t])
            start = None
    if start is not None:
        spans.append([start, len(rms_db) * hop_seconds])
    merged: list[list[float]] = []
    for s, e in spans:
        if merged and s - merged[-1][1] < MERGE_GAP:
            merged[-1][1] = e
        else:
            merged.append([s, e])
    return [[round(s, 3), round(e, 3)] for s, e in merged if e - s >= MIN_SPAN]


def _separate_vocals(audio_path: Path, workdir: Path) -> Path:
    cmd = [
        sys.executable, "-m", "demucs", "--two-stems=vocals", "-n", "htdemucs",
        "-d", "cpu", "-o", str(workdir), str(audio_path),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
    if r.returncode != 0:
        raise RuntimeError(f"demucs failed: {r.stderr.strip()[-500:]}")
    hits = list(workdir.rglob("vocals.wav"))
    if not hits:
        raise RuntimeError("demucs produced no vocals.wav")
    return hits[0]


def _cache_file(audio_path: Path, cache_dir: Path) -> Path:
    st = audio_path.stat()
    key = hashlib.sha1(f"{str(audio_path).lower()}|{st.st_size}|{int(st.st_mtime)}".encode()).hexdigest()[:16]
    return cache_dir / f"analysis-{key}.json"


def cached(audio_path: str | Path, cache_dir: str | Path) -> dict | None:
    """The cached analysis for this file, or None if it hasn't been run (#6867)."""
    cache = _cache_file(Path(audio_path), Path(cache_dir))
    return json.loads(cache.read_text(encoding="utf-8")) if cache.exists() else None


def analyze(audio_path: str | Path, cache_dir: str | Path) -> dict:
    """{duration, tempo, beats, vocal_spans} for one song, cached on disk."""
    import librosa
    import numpy as np

    audio_path = Path(audio_path)
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache = _cache_file(audio_path, cache_dir)
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))

    y, sr = librosa.load(str(audio_path), sr=22050, mono=True)
    duration = len(y) / sr
    tempo, beats = librosa.beat.beat_track(y=y, sr=sr, units="time")
    logger.info("music video analysis: %s %.1fs %.0f BPM", audio_path.name, duration, float(np.atleast_1d(tempo)[0]))

    with tempfile.TemporaryDirectory() as tmp:
        vocals = _separate_vocals(audio_path, Path(tmp))
        v, vsr = librosa.load(str(vocals), sr=22050, mono=True)
    hop = 512
    rms = librosa.feature.rms(y=v, hop_length=hop)[0]
    rms_db = librosa.amplitude_to_db(rms, ref=1.0)
    spans = vocal_spans_from_rms(list(map(float, rms_db)), hop / vsr)

    result = {
        "duration": round(duration, 3),
        "tempo": round(float(np.atleast_1d(tempo)[0]), 1),
        "beats": [round(float(b), 3) for b in beats],
        "vocal_spans": spans,
    }
    cache.write_text(json.dumps(result), encoding="utf-8")
    return result
