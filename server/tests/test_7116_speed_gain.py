"""#7116: speed-aware ride volume reference — stops turn it down, fast turns it up, lights don't pump."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from speed_gain import SpeedGain  # noqa: E402


def run(samples, gain=None):
    gain = gain or SpeedGain()
    return gain, [gain.update(t, v) for t, v in samples]


def test_a_real_stop_turns_it_down_after_the_dwell():
    _, out = run([(t, 0.3) for t in range(0, 12)])
    assert out[:8] == [0] * 8 and out[-1] == -2


def test_rolling_through_a_light_does_not_pump():
    samples = [(t, 0.5) for t in range(0, 6)] + [(t, 4.0) for t in range(6, 30)]
    gain, out = run(samples)
    assert set(out) == {0} and gain.history == []


def test_fast_stretch_turns_it_up_and_back():
    samples = [(t, 7.0) for t in range(0, 12)] + [(t, 4.0) for t in range(12, 20)]
    gain, out = run(samples)
    assert 1 in out and out[-1] == 0
    assert [s for _, s in gain.history] == ["fast", "cruise"]


def test_speed_in_the_dead_band_holds_the_state():
    gain, _ = run([(t, 7.0) for t in range(0, 12)])
    _, out = run([(t, 5.5) for t in range(12, 60)], gain)
    assert set(out) == {1}


def test_missing_fix_holds():
    gain, _ = run([(t, 0.2) for t in range(0, 10)])
    assert gain.update(11, None) == -2
