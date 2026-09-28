"""Self-synthesized Westminster quarter chimes (#5499).

Additive bell synthesis with numpy, written as 16-bit mono WAV with the stdlib
`wave` module. No third-party audio, recordings or samples anywhere: every
sample comes from the partial table below. The Westminster Quarters melody
(1793) is in the public domain.

The phone plays these on its bed layer (the #1728 sleep-engine player), which
LOOPS its clip until a bed_stop arrives. Every clip therefore ends in
TAIL_PAD_S of digital silence, so a bed_stop that lands a little late stops
silence instead of re-striking the bell.

Regenerate the committed clips with server/scripts/generate_chimes.py.
"""

from __future__ import annotations

import json
import wave
from pathlib import Path

import numpy as np

SAMPLE_RATE = 16000

NOTE_FREQS = {"G#4": 415.30, "F#4": 369.99, "E4": 329.63, "B3": 246.94}
HOUR_BELL_FREQ = 164.81  # E3

# The five changes of the Westminster Quarters, and which play at each quarter.
CHANGES = {
    1: ["G#4", "F#4", "E4", "B3"],
    2: ["E4", "G#4", "F#4", "B3"],
    3: ["E4", "F#4", "G#4", "E4"],
    4: ["G#4", "E4", "F#4", "B3"],
    5: ["B3", "F#4", "G#4", "E4"],
}
QUARTERS = {15: [1], 30: [2, 3], 45: [4, 5, 1], 0: [2, 3, 4, 5]}

NOTE_INTERVAL_S = 0.7
CHANGE_GAP_S = 0.7
RING_S = 3.0
HOUR_STRIKE_INTERVAL_S = 2.0
TAIL_PAD_S = 3.0
QUARTER_AMPLITUDE = 0.8
HOUR_AMPLITUDE = 1.0

# (ratio to the strike note, amplitude, decay seconds): hum, prime, minor-third
# tierce, quint, nominal and upper partials, the classic church-bell spectrum.
PARTIALS = [
    (0.5, 0.30, 2.8),
    (1.0, 1.00, 2.0),
    (1.2, 0.45, 1.4),
    (1.5, 0.30, 1.1),
    (2.0, 0.50, 0.9),
    (2.5, 0.20, 0.6),
    (3.0, 0.12, 0.45),
    (4.2, 0.06, 0.25),
]
ATTACK_S = 0.004

LICENSE_TEXT = """Audiplex chime clips (q00.wav, q15.wav, q30.wav, q45.wav, and any hour_NN.wav)

These clips are self-synthesized by server/audiplex/chime_synth.py using
additive synthesis with numpy. They contain no third-party audio, recordings
or samples. The Westminster Quarters melody (1793) is in the public domain.

The clips may be used, copied and modified freely.
"""


def bell(freq: float, dur_s: float, sr: int = SAMPLE_RATE) -> np.ndarray:
    """One bell strike at `freq`, `dur_s` long, peak-normalized to 1.0."""
    n = max(1, int(round(dur_s * sr)))
    t = np.arange(n, dtype=np.float64) / sr
    out = np.zeros(n, dtype=np.float64)
    for i, (ratio, amp, decay) in enumerate(PARTIALS):
        f = freq * ratio
        if f >= sr / 2:
            continue
        phase = 0.37 * i  # fixed, so the output is deterministic
        out += amp * np.sin(2 * np.pi * f * t + phase) * np.exp(-t / decay)
    attack = min(n, max(1, int(ATTACK_S * sr)))
    out[:attack] *= np.linspace(0.0, 1.0, attack)
    peak = np.max(np.abs(out))
    return out / peak if peak > 0 else out


def render_sequence(onsets: list[tuple[float, float, float]], sound_end_s: float) -> np.ndarray:
    """Mix bells given as (onset_s, freq, amplitude) into one buffer."""
    total = int(round(sound_end_s * SAMPLE_RATE))
    buf = np.zeros(total, dtype=np.float64)
    for onset, freq, amp in onsets:
        start = int(round(onset * SAMPLE_RATE))
        if start >= total:
            continue
        tone = bell(freq, (total - start) / SAMPLE_RATE)
        buf[start:start + len(tone)] += amp * tone[: total - start]
    return buf


def _quarter_onsets(minute: int) -> list[tuple[float, float, float]]:
    if minute not in QUARTERS:
        raise ValueError(f"no Westminster quarter at minute {minute}")
    onsets = []
    t = 0.0
    for c, change in enumerate(QUARTERS[minute]):
        if c:
            t += CHANGE_GAP_S
        for note in CHANGES[change]:
            onsets.append((t, NOTE_FREQS[note], QUARTER_AMPLITUDE))
            t += NOTE_INTERVAL_S
    return onsets


def render_quarter(minute: int) -> tuple[np.ndarray, float]:
    """The quarter chime for `minute` (0/15/30/45), without the silent pad."""
    onsets = _quarter_onsets(minute)
    sound = onsets[-1][0] + RING_S
    return render_sequence(onsets, sound), sound


def render_hour(hour: int) -> tuple[np.ndarray, float]:
    """The :00 quarter followed by `hour` (1-12) strikes of the hour bell."""
    if not 1 <= hour <= 12:
        raise ValueError(f"hour must be 1-12, got {hour}")
    onsets = _quarter_onsets(0)
    first = onsets[-1][0] + 1.5
    strikes = [(first + i * HOUR_STRIKE_INTERVAL_S, HOUR_BELL_FREQ, HOUR_AMPLITUDE) for i in range(hour)]
    sound = strikes[-1][0] + RING_S + 1.0
    return render_sequence(onsets + strikes, sound), sound


def to_pcm16(samples: np.ndarray) -> bytes:
    """Peak-normalize to 0.89 and pack as little-endian int16."""
    peak = float(np.max(np.abs(samples))) if len(samples) else 0.0
    scaled = samples * (0.89 / peak) if peak > 0 else samples
    return (np.clip(scaled, -1.0, 1.0) * 32767).astype("<i2").tobytes()


def write_wav(path: Path, samples: np.ndarray, sound_seconds: float) -> dict:
    """Write `samples` plus the silent loop-guard pad; return the manifest entry."""
    path = Path(path)
    padded = np.concatenate([samples, np.zeros(int(TAIL_PAD_S * SAMPLE_RATE))])
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(to_pcm16(padded))
    return {
        "file": path.name,
        "sound_seconds": round(sound_seconds, 3),
        "total_seconds": round(len(padded) / SAMPLE_RATE, 3),
    }


def generate_assets(out_dir: Path) -> dict:
    """Write q00/q15/q30/q45.wav, manifest.json and LICENSE.txt into out_dir."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = {}
    for minute in (0, 15, 30, 45):
        samples, sound = render_quarter(minute)
        name = f"q{minute:02d}"
        manifest[name] = write_wav(out_dir / f"{name}.wav", samples, sound)
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (out_dir / "LICENSE.txt").write_text(LICENSE_TEXT, encoding="utf-8")
    return manifest


def ensure_hour_clip(hour: int, out_dir: Path) -> tuple[Path, float]:
    """The hour-strike clip for `hour`, rendered on first use and cached."""
    out_dir = Path(out_dir)
    path = out_dir / f"hour_{hour:02d}.wav"
    sidecar = out_dir / f"hour_{hour:02d}.json"
    if path.is_file() and sidecar.is_file():
        try:
            return path, float(json.loads(sidecar.read_text(encoding="utf-8"))["sound_seconds"])
        except (OSError, ValueError, KeyError, TypeError):
            pass
    out_dir.mkdir(parents=True, exist_ok=True)
    samples, sound = render_hour(hour)
    entry = write_wav(path, samples, sound)
    sidecar.write_text(json.dumps(entry), encoding="utf-8")
    return path, float(entry["sound_seconds"])
