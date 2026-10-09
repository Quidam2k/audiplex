"""#3504: gain_audit flags a track that kept the previous track's gain."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import gain_audit as ga  # noqa: E402

TARGET, SLIDER = -24.0, 0.8
LUFS = {1: -12.0, 2: -18.0, 3: -10.0, 4: -14.0, 5: -16.0, 6: -20.0, 7: None}


def seg(tid, level, start, n=4, first=None):
    rows = [{"at": start + i, "track_id": tid, "volume": level, "playing": True} for i in range(n)]
    if first is not None:
        rows[0]["volume"] = first
    return rows


def right(tid):
    return min(1.0, SLIDER * ga.gain(LUFS[tid], TARGET))


def test_all_correct_tracks_are_ok():
    rows = seg(1, right(1), 0) + seg(2, right(2), 10) + seg(3, right(3), 20)
    out = ga.audit(rows, LUFS, TARGET)
    assert [r["verdict"] for r in out] == ["ok"] * 3
    assert ga.estimate_slider(rows, LUFS, TARGET) == pytest.approx(SLIDER)


def test_track_started_under_a_duck_keeps_previous_gain():
    rows = (seg(1, right(1), 0) + seg(2, right(1), 10, first=0.3 * right(1))
            + seg(3, right(3), 20) + seg(4, right(4), 30) + seg(5, right(5), 40) + seg(6, right(6), 50))
    out = {r["track_id"]: r for r in ga.audit(rows, LUFS, TARGET)}
    assert out[2]["verdict"] == "stale_prev_gain" and out[2]["started_ducked"]
    assert out[2]["error_db"] == pytest.approx(-6.0, abs=0.1)
    assert all(out[t]["verdict"] == "ok" for t in (1, 3, 4, 5, 6))


def test_level_matching_nothing_is_a_mismatch():
    rows = seg(1, right(1), 0) + seg(3, right(3), 10) + seg(4, right(4), 20) + seg(5, 0.02, 30)
    assert ga.audit(rows, LUFS, TARGET)[-1]["verdict"] == "mismatch"


def test_other_diag_kinds_and_paused_rows_are_ignored():
    rows = seg(1, right(1), 0) + [
        {"kind": "cmd_queued", "at": 5, "track_id": 1, "volume": None},
        {"kind": "state", "at": 6, "track_id": 1, "volume": 0.99, "playing": False},
    ]
    out = ga.audit(rows, LUFS, TARGET)
    assert out[0]["rows"] == 4 and out[0]["verdict"] == "ok"


def test_unmeasured_track_gets_no_gain():
    assert ga.gain(None, TARGET) == 1.0
    assert ga.gain(-30.0, TARGET) == 1.0
    assert ga.gain(-4.0, TARGET) == 0.1
