"""Tempo (BPM) and musical key from mono samples, numpy only (#1002).

Filled into tracks.bpm / bpm_conf / beat_offset / musical_key / key_conf by
scripts/measure_tempo_key.py, used by harmonic.py to order a set so the next
song is in a compatible key and near the same tempo. Pure: no DB, no I/O.

Tempo: an onset-strength curve (how much the spectrum jumps up per frame) is
autocorrelated; the strongest repeat between 60 and 190 BPM wins, weighted
toward ~120 so a 128 BPM song isn't called 64 or 256. Known limits: half/double
confusion on sparse material, and rubato/live/classical music has no steady beat
(low bpm_conf).

Key: a 12-note "chroma" profile (how much of each pitch class sounds) is
correlated with the Krumhansl major/minor key profiles. Known limit: a key and
its relative minor/major share every note, so those get confused; Camelot treats
them as compatible anyway.

Camelot wheel: each key gets a number 1-12 and a letter (B = major, A = minor).
Neighbours on the wheel (same number, or one number up/down) mix smoothly.
"""

from __future__ import annotations

from typing import Any, Optional

import numpy as np

SR = 22050
HOP = 256                 # onset frames: ~86 per second
N_FFT = 2048
CHROMA_FFT = 8192         # ~2.7 Hz bins, fine enough to split semitones above ~65 Hz
CHROMA_LO, CHROMA_HI = 65.0, 2000.0
BPM_LO, BPM_HI = 60.0, 190.0
BPM_PRIOR = 120.0         # centre of the tempo preference (log-normal, 1 octave wide)

NOTES = ("C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B")
# Krumhansl-Kessler key profiles, index 0 = tonic.
MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])


def camelot(pc: int, minor: bool) -> str:
    """Camelot code for a key: C major -> 8B, A minor -> 8A."""
    if minor:
        pc = (pc + 3) % 12  # relative major shares the number
    return f"{(7 * pc + 7) % 12 + 1}{'A' if minor else 'B'}"


def key_name(code: str) -> str:
    """'8B' -> 'C major', '8A' -> 'A minor'. Unknown -> the code itself."""
    for pc in range(12):
        for minor in (False, True):
            if camelot(pc, minor) == code:
                return f"{NOTES[pc]} {'minor' if minor else 'major'}"
    return code


def _frames(x: np.ndarray, n: int, hop: int) -> np.ndarray:
    if len(x) < n:
        x = np.pad(x, (0, n - len(x)))
    count = 1 + (len(x) - n) // hop
    return np.lib.stride_tricks.as_strided(
        x, shape=(count, n), strides=(x.strides[0] * hop, x.strides[0]), writeable=False)


def _spectrum(x: np.ndarray, n: int, hop: int, chunk: int = 512) -> np.ndarray:
    """Magnitude STFT (frames x bins), float32, computed in chunks to bound memory."""
    fr = _frames(x.astype(np.float32), n, hop)
    win = np.hanning(n).astype(np.float32)
    out = [np.abs(np.fft.rfft(fr[i:i + chunk] * win, axis=1)).astype(np.float32)
           for i in range(0, len(fr), chunk)]
    return np.concatenate(out) if out else np.zeros((0, n // 2 + 1), np.float32)


def onset_envelope(x: np.ndarray, sr: int = SR) -> np.ndarray:
    """Spectral flux per HOP frame, local mean removed, clipped at 0."""
    mag = _spectrum(x, N_FFT, HOP)
    if len(mag) < 2:
        return np.zeros(0)
    logm = np.log1p(1000.0 * mag)
    flux = np.maximum(np.diff(logm, axis=0), 0).sum(axis=1).astype(np.float64)
    w = max(1, int(round(0.5 * sr / HOP)))  # ~0.5 s moving average
    flux -= np.convolve(flux, np.ones(w) / w, mode="same")
    return np.maximum(flux, 0)


def _peak(a: np.ndarray, i: int) -> float:
    """Parabolic-interpolated position of the peak at index i."""
    if 0 < i < len(a) - 1:
        d = a[i - 1] - 2 * a[i] + a[i + 1]
        if d < 0:
            return i + 0.5 * (a[i - 1] - a[i + 1]) / d
    return float(i)


def tempo(env: np.ndarray, sr: int = SR) -> tuple[Optional[float], float, Optional[float]]:
    """(bpm, confidence 0..1, first-beat offset in seconds) or (None, 0, None)."""
    fps = sr / HOP
    lo, hi = int(fps * 60 / BPM_HI), int(np.ceil(fps * 60 / BPM_LO))
    if len(env) < hi * 4 or not env.any():
        return None, 0.0, None
    e = env - env.mean()
    n = len(e)
    spec = np.fft.rfft(e, 2 * n)
    ac = np.fft.irfft(spec * np.conj(spec))[:n]
    if ac[0] <= 0:
        return None, 0.0, None
    ac /= ac[0]
    lags = np.arange(lo, hi + 1)
    bpms = 60 * fps / lags
    prior = np.exp(-0.5 * (np.log2(bpms / BPM_PRIOR)) ** 2)
    score = ac[lags] * prior
    best = int(lags[int(np.argmax(score))])
    conf = float(np.clip(ac[best], 0, 1))
    # Refine on the 4th repeat: 4x the lag resolution of the coarse peak.
    k = 4 if best * 4 + 4 < n else 1
    win = ac[best * k - k: best * k + k + 1]
    period = (best * k - k + _peak(win, int(np.argmax(win)))) / k
    bpm = 60 * fps / period
    # Beat phase: the offset whose comb of beats collects the most onset strength.
    p = int(round(period))
    idx = np.arange(0, len(env) - p, period)
    sums = [env[np.minimum((idx + ph).astype(int), len(env) - 1)].sum() for ph in range(p)]
    # A flux frame fires when a hit enters the end of its window: shift back to the hit.
    offset = (float(np.argmax(sums)) + (N_FFT - HOP / 2) / HOP) / fps % (60 / bpm)
    return round(float(bpm), 1), round(conf, 3), round(offset, 3)


def chroma(x: np.ndarray, sr: int = SR) -> np.ndarray:
    """12-bin pitch-class profile (C..B), each frame normalized so loud bits don't dominate."""
    mag = _spectrum(x, CHROMA_FFT, CHROMA_FFT // 2)
    freqs = np.fft.rfftfreq(CHROMA_FFT, 1 / sr)
    band = (freqs >= CHROMA_LO) & (freqs <= CHROMA_HI)
    pcs = (np.round(12 * np.log2(freqs[band] / 440.0)).astype(int) + 9) % 12  # 0 = C
    frames = np.zeros((len(mag), 12))
    for pc in range(12):
        frames[:, pc] = mag[:, band][:, pcs == pc].sum(axis=1)
    tot = frames.sum(axis=1, keepdims=True)
    keep = tot[:, 0] > 1e-6 * max(1e-12, tot.max())
    if not keep.any():
        return np.zeros(12)
    return (frames[keep] / tot[keep]).mean(axis=0)


def key(ch: np.ndarray) -> tuple[Optional[str], float, float]:
    """(Camelot code, correlation 0..1, margin over the runner-up) or (None, 0, 0)."""
    if not np.any(ch) or np.std(ch) == 0:
        return None, 0.0, 0.0
    scores = []
    for pc in range(12):
        for minor, prof in ((False, MAJOR), (True, MINOR)):
            r = float(np.corrcoef(ch, np.roll(prof, pc))[0, 1])
            scores.append((r, pc, minor))
    scores.sort(reverse=True)
    r, pc, minor = scores[0]
    return camelot(pc, minor), round(max(0.0, r), 3), round(r - scores[1][0], 3)


def analyze(samples: np.ndarray, sr: int = SR, start_s: float = 0.0) -> dict[str, Any]:
    """Everything for one track. start_s = where `samples` begin in the file,
    so beat_offset is measured from the track start (modulo one beat)."""
    x = np.asarray(samples, dtype=np.float64)
    bpm, bpm_conf, off = tempo(onset_envelope(x, sr), sr)
    if bpm and off is not None:
        beat = 60.0 / bpm
        off = round((start_s + off) % beat, 3)
    code, key_conf, margin = key(chroma(x, sr))
    return {"bpm": bpm, "bpm_conf": bpm_conf, "beat_offset": off,
            "musical_key": code, "key_conf": key_conf, "key_margin": margin,
            "seconds": round(len(x) / sr, 1)}
