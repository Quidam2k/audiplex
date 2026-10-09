"""#3981: dip texture labels on synthetic 4 s signals at 16 kHz."""

import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from scipy import signal

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import dip_texture as dt  # noqa: E402

SR = dt.SR
N = 4 * SR
T = np.arange(N) / SR


def _db(x, dbfs):
    return x * (10 ** (dbfs / 20) / np.sqrt(np.mean(x * x)))


def test_near_silence_is_silence():
    x = np.random.default_rng(1).standard_normal(N) * 1e-5
    assert dt.label_window(x)["texture"] == "silence"


def test_sustained_pad_is_ambient_and_bands_sum_to_one():
    x = _db(np.sin(2 * np.pi * 220 * T) + np.sin(2 * np.pi * 330 * T), -20)
    out = dt.label_window(x)
    assert out["texture"] == "ambient"
    assert out["low"] + out["mid"] + out["high"] == pytest.approx(1.0, abs=1e-6)


def test_kick_and_clicks_are_rhythm_only():
    rng = np.random.default_rng(2)
    x = np.zeros(N)
    for start in range(0, N, SR // 4):
        n = min(SR // 5, N - start)
        x[start:start + n] += np.sin(2 * np.pi * 60 * T[:n]) * np.exp(-T[:n] * 25)
        click = min(SR * 5 // 1000, N - start)
        x[start:start + click] += rng.standard_normal(click) * 0.5
    assert dt.label_window(_db(x, -20))["texture"] == "rhythm_only"


def test_syllable_rate_band_noise_is_vocal_likely():
    rng = np.random.default_rng(3)
    sos = signal.butter(4, [300, 3400], btype="bandpass", fs=SR, output="sos")
    band = signal.sosfiltfilt(sos, rng.standard_normal(N))
    envelope = 0.5 - 0.5 * np.cos(2 * np.pi * 4.5 * T)
    assert dt.label_window(_db(band * envelope, -20))["texture"] == "vocal_likely"


def test_empty_input_is_finite_silence():
    f = dt.features(np.empty(0, dtype=np.float32))
    assert all(np.isfinite(v) for v in f.values())
    assert dt.classify(f)[0] == "silence"


def test_zero_length_window_never_calls_ffmpeg(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("ffmpeg called")
    monkeypatch.setattr(subprocess, "run", boom)
    assert dt.decode_window("x.m4a", 5.0, 5.0).size == 0
