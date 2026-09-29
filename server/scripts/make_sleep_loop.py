"""Turn a recorded sleep track into a seamless loop for the sleep bed (#5889).

    python server/scripts/make_sleep_loop.py IN.m4a OUT.m4a [--title T] [--measure]

A recorded track isn't made to loop: Todd's "Starship Sleeping Quarters" opens
with 73 ms of silence and a ~0.75 s fade-in, and ends in a fade to zeros, so
looping it leaves an audible "breath" every hour. This trims those edges (to
where the level first/last comes within 3 dB of the body), then applies the
same loop seam as noise_synth: the first SEAM_S of the output is an equal-power
crossfade from what follows the kept tail into the head, so the end flows back
into the start. EDGE_RAMP_S ramps absorb any codec/container gap at the loop
point. No level change. Only the edges are decoded into memory; the hour-long
middle is stream-copied through ffmpeg's filter graph.

--measure prints the loop's edge levels and the seam step (listening-free check).
"""

import argparse
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np

SERVER_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER_DIR))

from audiplex.noise_synth import EDGE_RAMP_S, SEAM_S  # noqa: E402

SR = 44100
CH = 2
WIN = SR // 100  # 10 ms level windows
EDGE_S = 8.0  # how much of each end to inspect


def _decode(path: Path, *pre: str) -> np.ndarray:
    raw = subprocess.run(
        ["ffmpeg", "-loglevel", "error", *pre, "-i", str(path), "-f", "s16le",
         "-ac", str(CH), "-ar", str(SR), "-"],
        capture_output=True, check=True,
    ).stdout
    return np.frombuffer(raw, np.int16).reshape(-1, CH).astype(np.float32) / 32768


def _count_samples(path: Path) -> int:
    proc = subprocess.Popen(
        ["ffmpeg", "-loglevel", "error", "-i", str(path), "-f", "s16le", "-ac", str(CH),
         "-ar", str(SR), "-"], stdout=subprocess.PIPE,
    )
    total = 0
    while chunk := proc.stdout.read(1 << 22):
        total += len(chunk)
    if proc.wait():
        raise RuntimeError(f"ffmpeg failed decoding {path}")
    return total // (2 * CH)


def _window_db(x: np.ndarray) -> np.ndarray:
    n = len(x) // WIN
    frames = x[: n * WIN].reshape(n, WIN * CH).astype(np.float64)
    return 10 * np.log10(np.mean(frames ** 2, axis=1) + 1e-20)


def _db(x: np.ndarray) -> float:
    return float(10 * np.log10(np.mean(x.astype(np.float64) ** 2) + 1e-20))


def _write_wav(x: np.ndarray, path: Path) -> None:
    pcm = (np.clip(x, -1, 1) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(CH)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


def make_loop(src: Path, out: Path, title: str) -> None:
    total = _count_samples(src)
    head = _decode(src, "-t", str(EDGE_S))
    tail = _decode(src, "-sseof", f"-{EDGE_S}")
    tail_start = total - len(tail)

    body_db = float(np.median(np.concatenate([_window_db(head), _window_db(tail)])))
    ok_head = np.nonzero(_window_db(head) >= body_db - 3)[0]
    ok_tail = np.nonzero(_window_db(tail) >= body_db - 3)[0]
    start = int(ok_head[0]) * WIN  # first sample kept
    end = tail_start + (int(ok_tail[-1]) + 1) * WIN  # one past the last kept
    seam = int(SEAM_S * SR)
    if end - start < 4 * seam:
        raise ValueError("track too short to loop")

    # out[:seam] fades from what follows the kept middle (the kept tail) into the head.
    w = np.linspace(0.0, np.pi / 2, seam, dtype=np.float32)[:, None]
    tail_seg = tail[end - seam - tail_start : end - tail_start]
    head_seg = head[start : start + seam]
    xf = tail_seg * np.cos(w) + head_seg * np.sin(w)
    r = int(EDGE_RAMP_S * SR)
    xf[:r] *= np.linspace(0.0, 1.0, r, dtype=np.float32)[:, None]

    mid_a, mid_b = start + seam, end - seam
    ramp_st = (mid_b - mid_a - r) / SR
    print(f"body {body_db:.1f} dB; trim head {start / SR:.3f} s, tail {(total - end) / SR:.3f} s; "
          f"loop {(end - start - seam) / SR:.1f} s")
    with tempfile.TemporaryDirectory() as tmp:
        xf_wav = Path(tmp) / "seam.wav"
        _write_wav(xf, xf_wav)
        graph = (
            f"[1:a]aresample={SR},aformat=channel_layouts=stereo,"
            f"atrim=start_sample={mid_a}:end_sample={mid_b},asetpts=PTS-STARTPTS,"
            f"afade=t=out:st={ramp_st:.6f}:d={EDGE_RAMP_S}[m];[0:a][m]concat=n=2:v=0:a=1[o]"
        )
        out.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(xf_wav), "-i", str(src),
             "-filter_complex", graph, "-map", "[o]", "-c:a", "aac", "-b:a", "128k",
             "-metadata", f"title={title}", "-metadata", "comment=seamless sleep loop (#5889)",
             str(out)],
            check=True,
        )


def measure(path: Path) -> None:
    head = _decode(path, "-t", "3")
    tail = _decode(path, "-sseof", "-3")
    print("head dB per 0.25 s:", [round(_db(head[i:i + SR // 4]), 1) for i in range(0, len(head), SR // 4)])
    print("tail dB per 0.25 s:", [round(_db(tail[i:i + SR // 4]), 1) for i in range(0, len(tail), SR // 4)])
    step = np.abs(head[0] - tail[-1]).max()
    typical = np.percentile(np.abs(np.diff(tail, axis=0)), 99)
    print(f"seam step {step:.4f} (p99 step within the tail {typical:.4f})")
    stats = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(path), "-af", "volumedetect",
                            "-f", "null", "-"], capture_output=True, text=True).stderr
    for line in stats.splitlines():
        if "mean_volume" in line or "max_volume" in line:
            print(line.split("]")[-1].strip())
    dur = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "default=nw=1:nk=1", str(path)], capture_output=True, text=True).stdout
    print(f"duration {float(dur):.3f} s")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("src", type=Path)
    ap.add_argument("out", type=Path, nargs="?")
    ap.add_argument("--title", default=None)
    ap.add_argument("--measure", action="store_true", help="only measure SRC")
    args = ap.parse_args()
    if args.measure:
        measure(args.src)
        return 0
    if not args.out:
        ap.error("OUT is required unless --measure")
    make_loop(args.src, args.out, args.title or args.src.stem)
    measure(args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
