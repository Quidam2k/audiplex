"""Pantheon #5790 — RIFF-wrapped MP3 unwrap: lossless, refuses non-MP3 RIFF, skips a playing file."""
import shutil
import sqlite3
import struct
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import unwrap_riff_mp3 as u  # noqa: E402

FFPROBE = shutil.which("ffprobe")
_sib = Path(FFPROBE).with_name(Path(FFPROBE).name.replace("ffprobe", "ffmpeg")) if FFPROBE else None
FFMPEG = str(_sib) if _sib and _sib.exists() else shutil.which("ffmpeg")  # full build has libmp3lame
needs_ff = pytest.mark.skipif(not (FFMPEG and FFPROBE), reason="ffmpeg/ffprobe not installed")


def _id3(payload=b"TIT2\x00\x00\x00\x05\x00\x00test"):
    n = len(payload)
    size = bytes([(n >> 21) & 0x7F, (n >> 14) & 0x7F, (n >> 7) & 0x7F, n & 0x7F])
    return b"ID3\x03\x00\x00" + size + payload


def _riff(frames, fmt_tag=0x55):
    fmt = struct.pack("<HHIIHH", fmt_tag, 2, 44100, 16000, 1, 0)
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(frames)) + frames
    return b"RIFF" + struct.pack("<I", len(body)) + body


def test_split_finds_frames_and_tag():
    tag, frames = _id3(), b"\xff\xfb" + b"x" * 100
    assert u.split_riff_mp3(tag + _riff(frames)) == (tag, frames)


def test_non_mp3_riff_and_plain_mp3_refused():
    assert u.split_riff_mp3(_id3() + _riff(b"pcm" * 10, fmt_tag=1)) is None
    assert u.split_riff_mp3(_id3() + b"\xff\xfb" + b"x" * 100) is None


def _mp3(tmp_path):
    src = tmp_path / "tone.mp3"
    subprocess.run([FFMPEG, "-v", "error", "-f", "lavfi", "-i", "sine=f=440:d=3", "-c:a", "libmp3lame",
                    "-id3v2_version", "0", str(src)], check=True)
    return src.read_bytes()


def _db(tmp_path, path, secs):
    db = tmp_path / "a.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE tracks (id INTEGER, file_path TEXT, duration_seconds REAL, file_size INT, file_hash TEXT)")
    con.execute("INSERT INTO tracks VALUES (1, ?, ?, 0, 'old')", (str(path), secs))
    con.commit(); con.close()
    return db


@needs_ff
def test_apply_unwraps_losslessly_and_restore_puts_it_back(tmp_path):
    frames = _mp3(tmp_path)
    track = tmp_path / "song.mp3"
    original = _id3() + _riff(frames)
    track.write_bytes(original)
    db = _db(tmp_path, track, 3.0)
    rc = u.main(["--ids", "1", "--db", str(db), "--apply", "--backup-root", str(tmp_path / "bk"),
                 "--ffprobe", FFPROBE], playing_fn=lambda: set())
    assert rc == 0
    assert track.read_bytes() == _id3() + frames
    fmt, secs = u.probe(FFPROBE, str(track))
    assert "mp3" in fmt and abs(secs - 3.0) < 0.5
    size, h = sqlite3.connect(db).execute("SELECT file_size, file_hash FROM tracks").fetchone()
    assert size == track.stat().st_size and h == u.scanner_hash(str(track))
    bk = next((tmp_path / "bk").iterdir())
    assert u.main(["--restore", str(bk), "--db", str(db)]) == 0
    assert track.read_bytes() == original


@needs_ff
def test_playing_track_is_skipped_and_unreadable_state_refuses_apply(tmp_path):
    track = tmp_path / "song.mp3"
    original = _id3() + _riff(_mp3(tmp_path))
    track.write_bytes(original)
    db = _db(tmp_path, track, 3.0)
    import os
    u.main(["--ids", "1", "--db", str(db), "--apply", "--backup-root", str(tmp_path / "bk")],
           playing_fn=lambda: {os.path.normcase(str(track))})
    assert track.read_bytes() == original
    assert u.main(["--ids", "1", "--db", str(db), "--apply"], playing_fn=lambda: None) == 2
    assert track.read_bytes() == original
