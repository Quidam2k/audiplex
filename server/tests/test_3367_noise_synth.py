"""#3367: the fallback brown-noise sleep bed is seamless, quiet and brown."""

import numpy as np
import pytest

pytest.importorskip("scipy")

from audiplex import noise_synth  # noqa: E402

SR = noise_synth.SAMPLE_RATE


@pytest.fixture(scope="module")
def loop():
    return noise_synth.brown_noise(30, edge_ramp=False)


def test_loop_seam_is_no_bigger_than_an_ordinary_step(loop):
    steps = np.abs(np.diff(loop))
    seam = abs(float(loop[0]) - float(loop[-1]))
    assert seam <= np.percentile(steps, 99.9)


def test_level_is_low_and_bounded(loop):
    rms_db = 20 * np.log10(np.sqrt(np.mean(loop.astype(np.float64) ** 2)))
    peak_db = 20 * np.log10(np.max(np.abs(loop)))
    assert abs(rms_db - noise_synth.TARGET_RMS_DBFS) < 0.5
    assert peak_db <= noise_synth.PEAK_CEILING_DBFS + 0.01
    assert abs(float(loop.mean())) < 1e-3


def test_spectrum_is_brown_not_white(loop):
    spec = np.abs(np.fft.rfft(loop)) ** 2
    freqs = np.fft.rfftfreq(len(loop), 1 / SR)
    low = spec[(freqs > 50) & (freqs < 200)].mean()
    high = spec[(freqs > 2000) & (freqs < 8000)].mean()
    assert low / high > 100  # ~-6 dB/octave, so far more energy down low


def test_edge_ramp_starts_and_ends_at_silence():
    ramped = noise_synth.brown_noise(10)
    assert ramped[0] == 0.0 and ramped[-1] == 0.0


def test_deterministic():
    assert np.array_equal(noise_synth.brown_noise(2), noise_synth.brown_noise(2))
