"""Regression tests for the add-only meditation watcher (#9295)."""

import os

import pytest

from audiplex.meditation_watch import MeditationWatch


@pytest.fixture
def watcher(tmp_path):
    scans = []
    watch = MeditationWatch(
        roots_fn=lambda: [str(tmp_path)],
        scan_fn=scans.append,
    )
    return watch, scans


def test_new_file_scanned_once(tmp_path, watcher):
    watch, scans = watcher
    (tmp_path / "meditation.m4a").write_bytes(b"audio")

    assert watch.tick() == "unstable"
    assert scans == []
    assert watch.tick() == "scanned"
    assert scans == [[str(tmp_path)]]
    assert watch.tick() == "idle"
    assert len(scans) == 1


def test_growing_file_waits_for_stable_snapshot(tmp_path, watcher):
    watch, scans = watcher
    audio = tmp_path / "growing.m4a"
    audio.write_bytes(b"first")
    original_stat = audio.stat()

    assert watch.tick() == "unstable"

    with audio.open("ab") as stream:
        stream.write(b"more audio")
    os.utime(
        audio,
        ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns + 1_000_000_000),
    )
    assert audio.stat().st_mtime_ns != original_stat.st_mtime_ns

    assert watch.tick() == "unstable"
    assert scans == []
    assert watch.tick() == "scanned"
    assert scans == [[str(tmp_path)]]


def test_empty_folder_is_skipped(watcher):
    watch, scans = watcher

    for _ in range(4):
        assert watch.tick() == "skip"
    assert scans == []


def test_missing_folder_is_skipped(tmp_path):
    scans = []
    watch = MeditationWatch(
        roots_fn=lambda: [str(tmp_path / "missing")],
        scan_fn=scans.append,
    )

    for _ in range(3):
        assert watch.tick() == "skip"
    assert scans == []


def test_identical_file_restored_after_empty_folder_is_idle(tmp_path, watcher):
    watch, scans = watcher
    audio = tmp_path / "restored.m4a"
    content = b"same meditation audio"
    audio.write_bytes(content)
    original_stat = audio.stat()

    assert watch.tick() == "unstable"
    assert watch.tick() == "scanned"

    audio.unlink()
    assert watch.tick() == "skip"

    audio.write_bytes(content)
    os.utime(audio, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

    assert watch.tick() == "unstable"
    assert watch.tick() == "idle"
    assert scans == [[str(tmp_path)]]


def test_failed_scan_retries_next_tick(tmp_path):
    (tmp_path / "retry.m4a").write_bytes(b"audio")
    scans = []
    should_fail = True

    def scan(roots):
        scans.append(roots)
        if should_fail:
            raise RuntimeError("temporary scan failure")

    watch = MeditationWatch(
        roots_fn=lambda: [str(tmp_path)],
        scan_fn=scan,
    )

    assert watch.tick() == "unstable"
    assert watch.tick() == "error"
    assert scans == [[str(tmp_path)]]

    should_fail = False
    assert watch.tick() == "scanned"
    assert scans == [[str(tmp_path)], [str(tmp_path)]]
    assert watch.tick() == "idle"
    assert len(scans) == 2


def test_no_roots():
    scans = []
    watch = MeditationWatch(roots_fn=lambda: [], scan_fn=scans.append)

    assert watch.tick() == "no-roots"
    assert scans == []


def test_text_file_alone_is_skipped(tmp_path, watcher):
    watch, scans = watcher
    (tmp_path / "notes.txt").write_text("Not audio.", encoding="utf-8")

    for _ in range(3):
        assert watch.tick() == "skip"
    assert scans == []


class TestMeditationOnlyScan:
    """POST /api/library/scan/meditations: meditation family only, never on an empty folder."""

    def _settings(self, tmp_path, monkeypatch, scan):
        from audiplex.config import LibraryRoot, Settings
        from audiplex.routers import library

        settings = Settings(library_roots=[
            LibraryRoot(path=str(tmp_path / "books"), category="audiobook_clean"),
            LibraryRoot(path=str(tmp_path / "music"), category="music"),
            LibraryRoot(path=str(tmp_path / "med"), category="meditation"),
        ])
        (tmp_path / "med").mkdir()
        monkeypatch.setattr(library, "get_settings", lambda: settings)
        monkeypatch.setattr(library, "scan_library", scan)

    def test_passes_only_meditation_roots(self, client, tmp_path, monkeypatch):
        from audiplex.schemas import ScanResultSchema

        seen = {}

        def fake_scan(db, roots, cover_dir):
            seen["roots"] = [(r.category, r.path) for r in roots]
            return ScanResultSchema(added=1, updated=0, removed=0, errors=[])

        self._settings(tmp_path, monkeypatch, fake_scan)
        (tmp_path / "med" / "calm.m4a").write_bytes(b"audio")
        r = client.post("/api/library/scan/meditations")
        assert r.status_code == 200
        assert r.json()["added"] == 1
        assert seen["roots"] == [("meditation", str(tmp_path / "med"))]

    def test_empty_folder_scans_and_sweeps_nothing(self, client, tmp_path, monkeypatch):
        self._settings(tmp_path, monkeypatch, lambda *a: pytest.fail("must not scan or sweep"))
        r = client.post("/api/library/scan/meditations")
        assert r.status_code == 200
        assert r.json()["removed"] == 0
        assert "nothing scanned or removed" in r.json()["errors"][0]


class TestWatcherSweep:
    """#5462: the watcher drops a meditation whose file is gone, never on an empty folder."""

    @pytest.fixture
    def real_watch(self, tmp_path, db_engine, monkeypatch):
        from unittest.mock import MagicMock, patch
        from sqlalchemy.orm import sessionmaker

        from audiplex import database, meditation_watch
        from audiplex.config import Settings

        monkeypatch.setattr(database, "_SessionLocal", sessionmaker(bind=db_engine))
        monkeypatch.setattr(meditation_watch, "get_settings",
                            lambda: Settings(cover_cache_dir=str(tmp_path / "covers")))
        med = tmp_path / "med"
        med.mkdir()
        audio = MagicMock()
        audio.info.length = 600.0
        audio.get.side_effect = lambda key, default=None: default
        with patch("audiplex.scanners.meditation.mutagen.File", return_value=audio):
            yield MeditationWatch(roots_fn=lambda: [str(med)]), med, sessionmaker(bind=db_engine)

    @staticmethod
    def _titles(Session):
        from audiplex.models import Book
        with Session() as s:
            return sorted(b.title for b in s.query(Book).filter(Book.category == "meditation"))

    def test_removed_file_drops_its_row(self, real_watch):
        watch, med, Session = real_watch
        (med / "Keep.m4a").write_bytes(b"a")
        (med / "Gone.m4a").write_bytes(b"b")
        assert [watch.tick(), watch.tick()] == ["unstable", "scanned"]
        assert self._titles(Session) == ["Gone", "Keep"]

        (med / "Gone.m4a").unlink()
        assert [watch.tick(), watch.tick()] == ["unstable", "scanned"]
        assert self._titles(Session) == ["Keep"]

    def test_emptied_folder_keeps_every_row(self, real_watch):
        watch, med, Session = real_watch
        (med / "Only.m4a").write_bytes(b"a")
        assert [watch.tick(), watch.tick()] == ["unstable", "scanned"]

        (med / "Only.m4a").unlink()
        for _ in range(3):
            assert watch.tick() == "skip"
        assert self._titles(Session) == ["Only"]

    def test_sweep_refuses_when_folder_empties_mid_tick(self, real_watch):
        watch, med, Session = real_watch
        with pytest.raises(RuntimeError):
            watch._scan_and_sweep([str(med)])
