"""#6710: scan_silence parses ffmpeg silencedetect output into tail / intro / gap findings."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from scan_silence import parse_silence  # noqa: E402


def test_tail_to_end_of_file():
    err = ("  Duration: 00:05:20.97, start: 0\n"
           "[s] silence_start: 251.031\n[s] silence_end: 320.969 | silence_duration: 69.9383\n")
    r = parse_silence(err)
    assert r["tail"] == (251.03, 69.94) and r["gaps"] == [] and r["intro"] == 0.0


def test_intro_and_unterminated_tail():
    err = ("  Duration: 00:03:21.83, start: 0\n"
           "[s] silence_start: 0\n[s] silence_end: 9.06583 | silence_duration: 9.06583\n"
           "[s] silence_start: 195.303\n")
    r = parse_silence(err)
    assert r["intro"] == 9.07 and r["tail"][0] == 195.3


def test_silence_then_sound_is_a_gap_not_a_tail():
    err = ("  Duration: 00:08:00.00, start: 0\n"
           "[s] silence_start: 200\n[s] silence_end: 230 | silence_duration: 30\n")
    r = parse_silence(err)
    assert r["tail"] is None and r["gaps"] == [(200.0, 30.0)]


def test_short_mid_silence_ignored():
    err = ("  Duration: 00:04:00.00, start: 0\n"
           "[s] silence_start: 100\n[s] silence_end: 103.5 | silence_duration: 3.5\n")
    assert parse_silence(err)["gaps"] == []
