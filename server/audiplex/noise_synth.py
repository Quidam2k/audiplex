"""Self-synthesized brown-noise sleep bed (#3367).

The fallback default for the sleep engine's bed layer (#1728) until Todd's own
brown-noise track ("Star Ship Sleeping Quarters") is copied onto this box. The
phone LOOPS the bed (REPEAT_MODE_ONE), so the file is built to be seamless:
the last sample flows straight into the first, because the head is an
equal-power crossfade of the noise that would have followed the tail.

Level is deliberately low for bedtime through a phone speaker: RMS
TARGET_RMS_DBFS with a hard ceiling at PEAK_CEILING_DBFS. A few ms of edge
ramp (EDGE_RAMP_S) turn any container/codec gap at the loop point into a
soft dip instead of a click.

Regenerate with server/scripts/generate_brown_noise.py.
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

SAMPLE_RATE = 22050
TARGET_RMS_DBFS = -26.0
PEAK_CEILING_DBFS = -12.0
LEAK = 0.998  # leaky integrator: brown (-6 dB/oct) above ~7 Hz, no DC drift
HIGHPASS_HZ = 20.0
SEAM_S = 5.0
EDGE_RAMP_S = 0.01


def _db(x: float) -> float:
    return 10 ** (x / 20)


def brown_noise(seconds: float, sr: int = SAMPLE_RATE, seed: int = 3367, edge_ramp: bool = True) -> np.ndarray:
    """A seamless mono loop, float32 in [-1, 1]."""
    from scipy.signal import butter, lfilter  # generator-only dep

    n = int(seconds * sr)
    seam = min(int(SEAM_S * sr), n // 2)
    rng = np.random.default_rng(seed)
    white = rng.standard_normal(n + seam).astype(np.float32)
    y = lfilter([1.0], [1.0, -LEAK], white).astype(np.float32)
    b, a = butter(2, HIGHPASS_HZ / (sr / 2), btype="highpass")
    y = lfilter(b, a, y).astype(np.float32)

    # Loop seam: out[k] fades from y[n+k] (what follows the tail) into y[k].
    w = np.linspace(0.0, np.pi / 2, seam, dtype=np.float32)
    out = y[:n].copy()
    out[:seam] = y[n:n + seam] * np.cos(w) + y[:seam] * np.sin(w)

    out -= out.mean()
    out *= _db(TARGET_RMS_DBFS) / float(np.sqrt(np.mean(out.astype(np.float64) ** 2)))
    ceiling = _db(PEAK_CEILING_DBFS)
    out = np.clip(out, -ceiling, ceiling)

    if edge_ramp:
        r = int(EDGE_RAMP_S * sr)
        ramp = np.linspace(0.0, 1.0, r, dtype=np.float32)
        out[:r] *= ramp
        out[-r:] *= ramp[::-1]
    return out


def write_wav(samples: np.ndarray, path: Path, sr: int = SAMPLE_RATE) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = np.round(np.clip(samples, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(sr)
        f.writeframes(pcm.tobytes())
    return path
