"""#5682: a track with end_trim_seconds streams a cut copy; the source is never touched."""
import os
import subprocess

import pytest

from audiplex import trimmed


@pytest.fixture
def src(tmp_path):
    p = tmp_path / "song.mp3"
    p.write_bytes(b"original-bytes")
    return str(p)


@pytest.fixture
def fake_ffmpeg(monkeypatch):
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        with open(argv[-1], "wb") as f:
            f.write(b"cut")
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(trimmed.subprocess, "run", run)
    return calls


@pytest.mark.parametrize("end", [None, 0, -3])
def test_no_trim_serves_source(src, tmp_path, fake_ffmpeg, end):
    assert trimmed.trimmed_path(1, src, end, str(tmp_path / "c")) == src
    assert fake_ffmpeg == []


def test_missing_source_passes_through(tmp_path, fake_ffmpeg):
    gone = str(tmp_path / "gone.mp3")
    assert trimmed.trimmed_path(1, gone, 10.0, str(tmp_path / "c")) == gone


def test_builds_once_then_reuses(src, tmp_path, fake_ffmpeg):
    cache = str(tmp_path / "c")
    a = trimmed.trimmed_path(7, src, 187.5, cache)
    b = trimmed.trimmed_path(7, src, 187.5, cache)
    assert a == b != src and open(a, "rb").read() == b"cut"
    assert len(fake_ffmpeg) == 1
    argv = fake_ffmpeg[0]
    assert argv[argv.index("-t") + 1] == "187.500"
    assert argv[argv.index("-c") + 1] == "copy"
    assert open(src, "rb").read() == b"original-bytes"


def test_changed_trim_rebuilds_and_drops_stale(src, tmp_path, fake_ffmpeg):
    cache = str(tmp_path / "c")
    old = trimmed.trimmed_path(7, src, 187.5, cache)
    new = trimmed.trimmed_path(7, src, 190.0, cache)
    assert old != new and not os.path.exists(old) and os.path.exists(new)
    other = trimmed.trimmed_path(70, src, 190.0, cache)  # id 70 is not id 7
    assert os.path.exists(new) and os.path.exists(other)


def test_ffmpeg_failure_serves_source_and_leaves_no_part(src, tmp_path, monkeypatch):
    def run(argv, **kw):
        open(argv[-1], "wb").write(b"half")
        return subprocess.CompletedProcess(argv, 1, b"", b"boom")

    monkeypatch.setattr(trimmed.subprocess, "run", run)
    cache = tmp_path / "c"
    assert trimmed.trimmed_path(3, src, 10.0, str(cache)) == src
    assert list(cache.iterdir()) == []
