"""Heuristic dip-texture labeling for #3981.
Describe the likely musical context within a quiet stretch.
Labels are hints, not facts; vocals and instruments can overlap.
Uses NumPy, SciPy, and ffmpeg for optional audio decoding.
"""

import math
import os
import subprocess

import numpy as np
from scipy import signal

SR = 16000

SILENCE_DB = -50.0  # Levels below this threshold are silence.
SILENCE_CONF_SPAN_DB = 10.0  # Confidence reaches 1 ten dB below silence.
VOCAL_SYLLABIC_MIN = 0.35  # Minimum speech-like modulation ratio.
VOCAL_SYLLABIC_FULL = 0.65  # Modulation ratio giving full vocal confidence.
MID_FRACTION_MIN = 0.5  # Minimum middle-band fraction for likely vocals.
RHYTHM_ONSET_MIN = 2.0  # Minimum onsets per second for rhythm-only texture.
RHYTHM_ONSET_FULL = 6.0  # Onset rate giving full rhythm confidence.
AMBIENT_ONSET_MAX = 0.6  # Rates below this threshold indicate ambient texture.

MIN_INPUT_S = 0.25  # Shorter inputs use default features.
MIN_SYLLABIC_S = 1.0  # Minimum duration for modulation analysis.
MAX_DECODE_S = 60.0  # Maximum duration decoded per call.
SPECTRAL_FRAME_S = 0.064  # Hann-frame duration for spectral features.
ONSET_FRAME_S = 0.032  # Hann-frame duration for spectral flux.
ONSET_HOP_S = 0.016  # Spectral-flux hop duration.
ONSET_DISTANCE_S = 0.080  # Minimum separation between onset peaks.
ONSET_MAD_FACTOR = 1.5  # Robust deviations required above median flux.
MAD_NORMAL_SCALE = 1.4826  # Convert MAD to a normal-distribution scale.
ONSET_FLUX_FLOOR = 1e-4  # Peak-normalized: steady-tone ripple ~2e-5, a soft kick ~4e-3.
FRAME_SKIP_DB = 40.0  # Ignore spectral frames this far below overall level.
MODULATION_SR = 100  # Envelope sample rate for modulation analysis.


def decode_window(
    path: str,
    start_s: float,
    end_s: float,
    ffmpeg: str = "ffmpeg",
    timeout: float = 120,
) -> np.ndarray:
    dur = min(MAX_DECODE_S, max(0.0, end_s - start_s))
    if dur <= 0:
        return np.empty(0, dtype=np.float32)
    flags = (
        getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0x4000)
        if os.name == "nt"
        else 0
    )
    result = subprocess.run(
        [
            ffmpeg,
            "-hide_banner", "-nostats", "-v", "error",
            "-ss", f"{start_s:.3f}", "-t", f"{dur:.3f}",
            "-i", path, "-map", "0:a:0",
            "-ac", "1", "-ar", str(SR), "-f", "f32le", "-",
        ],
        capture_output=True,
        timeout=timeout,
        creationflags=flags,
    )
    samples = np.frombuffer(result.stdout, dtype="<f4").astype(np.float32)
    if result.returncode != 0 and samples.size == 0:
        lines = result.stderr.decode("utf-8", errors="replace").strip().splitlines()
        raise ValueError(
            lines[-1] if lines else f"ffmpeg exited with code {result.returncode}"
        )
    return samples


def _defaults() -> dict[str, float]:
    return {
        "rms_db": -120.0,
        "flatness": 0.0,
        "low": 0.0,
        "mid": 0.0,
        "high": 0.0,
        "onset_rate": 0.0,
        "syllabic": 0.0,
    }


def _frames(x: np.ndarray, size: int, hop: int) -> np.ndarray:
    return np.lib.stride_tricks.sliding_window_view(x, size)[::hop]


def _syllabic(x: np.ndarray, sr: int) -> float:
    if x.size < MIN_SYLLABIC_S * sr:
        return 0.0
    upper = min(3400.0, 0.95 * sr / 2.0)
    if upper <= 300.0:
        return 0.0
    sos = signal.butter(4, [300.0, upper], btype="bandpass", fs=sr, output="sos")
    band = signal.sosfiltfilt(sos, x)
    envelope = np.abs(signal.hilbert(band))
    divisor = math.gcd(sr, MODULATION_SR)
    envelope = signal.resample_poly(
        envelope, MODULATION_SR // divisor, sr // divisor
    )
    envelope -= np.mean(envelope)
    power = np.abs(np.fft.rfft(envelope)) ** 2
    frequencies = np.fft.rfftfreq(envelope.size, 1.0 / MODULATION_SR)
    total = float(np.sum(power[(frequencies >= 0.5) & (frequencies <= 20.0)]))
    if total <= 1e-24:
        return 0.0
    syllables = float(np.sum(power[(frequencies >= 3.0) & (frequencies <= 7.0)]))
    return float(np.clip(syllables / total, 0.0, 1.0))


def features(x: np.ndarray, sr: int = SR) -> dict[str, float]:
    if sr <= 0:
        raise ValueError("sr must be positive")
    x = np.asarray(x, dtype=np.float64).ravel()
    result = _defaults()
    if x.size < MIN_INPUT_S * sr:
        return result
    x = np.nan_to_num(x, nan=0.0, posinf=0.0, neginf=0.0)
    peak = float(np.max(np.abs(x)))
    if peak == 0.0:
        result["rms_db"] = -240.0
        return result

    # Scaling keeps spectral calculations finite and independent of track gain.
    x = x / peak
    mean_square = float(np.mean(x * x))
    rms = peak * math.sqrt(mean_square)
    result["rms_db"] = float(20.0 * math.log10(rms + 1e-12))

    size = max(2, round(SPECTRAL_FRAME_S * sr))
    hop = max(1, size // 2)
    frames = _frames(x, size, hop)
    frame_energy = np.mean(frames * frames, axis=1)
    keep = frame_energy >= mean_square * 10.0 ** (-FRAME_SKIP_DB / 10.0)
    frames = frames[keep]
    if frames.size:
        window = signal.windows.hann(size, sym=False)
        power = np.abs(np.fft.rfft(frames * window, axis=1)) ** 2
        arithmetic = np.mean(power, axis=1)
        geometric = np.exp(np.mean(np.log(np.maximum(power, 1e-24)), axis=1))
        flatness = np.divide(
            geometric,
            arithmetic,
            out=np.zeros_like(arithmetic),
            where=arithmetic > 0.0,
        )
        result["flatness"] = float(np.mean(np.clip(flatness, 0.0, 1.0)))
        spectrum = np.sum(power, axis=0)
        total = float(np.sum(spectrum))
        if total > 0.0:
            frequencies = np.fft.rfftfreq(size, 1.0 / sr)
            result["low"] = float(np.sum(spectrum[frequencies < 250.0]) / total)
            result["mid"] = float(
                np.sum(spectrum[(frequencies >= 250.0) & (frequencies <= 4000.0)])
                / total
            )
            result["high"] = float(np.sum(spectrum[frequencies > 4000.0]) / total)

    size = max(2, round(ONSET_FRAME_S * sr))
    hop = max(1, round(ONSET_HOP_S * sr))
    frames = _frames(x, size, hop)
    window = signal.windows.hann(size, sym=False)
    magnitude = np.abs(np.fft.rfft(frames * window, axis=1)) / size
    log_magnitude = np.log1p(magnitude)
    flux = np.mean(np.maximum(np.diff(log_magnitude, axis=0), 0.0), axis=1)
    if flux.size >= 3:
        median = float(np.median(flux))
        mad = float(np.median(np.abs(flux - median)))
        threshold = max(
            ONSET_FLUX_FLOOR,
            median + ONSET_MAD_FACTOR * MAD_NORMAL_SCALE * mad,
        )
        distance = max(1, math.ceil(ONSET_DISTANCE_S * sr / hop))
        peaks, _ = signal.find_peaks(flux, height=threshold, distance=distance)
        peaks = peaks[flux[peaks] > threshold]
        result["onset_rate"] = float(peaks.size / (x.size / sr))

    result["syllabic"] = _syllabic(x, sr)
    return {
        key: float(value) if math.isfinite(value) else _defaults()[key]
        for key, value in result.items()
    }


def classify(f: dict) -> tuple[str, float]:
    rms_db = float(f.get("rms_db", -120.0))
    syllabic = float(f.get("syllabic", 0.0))
    mid = float(f.get("mid", 0.0))
    onset_rate = float(f.get("onset_rate", 0.0))

    if rms_db < SILENCE_DB:
        label = "silence"
        conf = min(1.0, (SILENCE_DB - rms_db) / SILENCE_CONF_SPAN_DB + 0.5)
    elif syllabic >= VOCAL_SYLLABIC_MIN and mid >= MID_FRACTION_MIN:
        label = "vocal_likely"
        conf = 0.5 + 0.5 * (
            (syllabic - VOCAL_SYLLABIC_MIN)
            / (VOCAL_SYLLABIC_FULL - VOCAL_SYLLABIC_MIN)
        )
    elif onset_rate >= RHYTHM_ONSET_MIN and mid < MID_FRACTION_MIN:
        label = "rhythm_only"
        conf = 0.5 + 0.5 * (
            (onset_rate - RHYTHM_ONSET_MIN)
            / (RHYTHM_ONSET_FULL - RHYTHM_ONSET_MIN)
        )
    elif onset_rate < AMBIENT_ONSET_MAX:
        label = "ambient"
        conf = 0.5 + 0.5 * (AMBIENT_ONSET_MAX - onset_rate) / AMBIENT_ONSET_MAX
    else:
        label, conf = "sparse_instrumental", 0.4
    return label, round(float(np.clip(conf, 0.0, 1.0)), 2)


def label_window(x: np.ndarray, sr: int = SR) -> dict:
    f = features(x, sr)
    label, conf = classify(f)
    return {"texture": label, "texture_conf": conf, **f}
