"""#5448: a music root can exclude sub-folders (and its own loose files) so a
removable drive holding the full tree doesn't re-catalogue what was copied to a
permanent root."""

from unittest.mock import MagicMock, patch

import pytest
import yaml

from audiplex.config import LibraryRoot, set_library_roots_for_category
from audiplex.models import Album, Track
from audiplex.scanner import scan_library


@pytest.fixture
def mocked_mutagen():
    audio = MagicMock()
    audio.info.length = 200.0
    audio.get.side_effect = lambda key, default=None: default
    audio.__contains__ = lambda self, key: False
    with patch("audiplex.scanners.music.mutagen.File", return_value=audio):
        yield


def _touch(p):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"")


@pytest.fixture
def two_roots(tmp_path):
    full = tmp_path / "H" / "Music"
    _touch(full / "loose.m4a")
    _touch(full / "Individual" / "faster" / "a.m4a")
    _touch(full / "Tryout" / "b.m4a")
    _touch(full / "Artists & Albums" / "Mixed" / "Ren Faire" / "Mix 1" / "c.mp3")
    _touch(full / "Artists & Albums" / "Jazz" / "Miles" / "Blue" / "d.mp3")
    copy = tmp_path / "Q" / "Music"
    _touch(copy / "loose.m4a")
    _touch(copy / "Individual" / "faster" / "a.m4a")
    _touch(copy / "Artists & Albums" / "Mixed" / "Ren Faire" / "Mix 1" / "c.mp3")
    return full, copy


def _paths(db):
    return sorted(t.file_path.replace("\\", "/") for t in db.query(Track).all())


def test_excluded_folders_and_loose_files_not_catalogued(db_session, two_roots, mocked_mutagen):
    full, copy = two_roots
    roots = [
        LibraryRoot(path=str(copy), category="music"),
        LibraryRoot(
            path=str(full), category="music",
            exclude=[".", "Individual", "Artists & Albums/Mixed/Ren Faire"],
        ),
    ]
    scan_library(db_session, roots, str(copy.parent / "covers"))
    got = _paths(db_session)
    h = str(full).replace("\\", "/")
    q = str(copy).replace("\\", "/")
    assert f"{h}/Tryout/b.m4a" in got
    assert f"{h}/Artists & Albums/Jazz/Miles/Blue/d.mp3" in got
    assert f"{q}/Individual/faster/a.m4a" in got
    assert f"{q}/loose.m4a" in got
    assert not [p for p in got if p.startswith(h) and ("Individual" in p or "Ren Faire" in p)]
    assert f"{h}/loose.m4a" not in got
    # genre still comes from the path under the permanent copy
    ren = db_session.query(Album).filter(Album.folder_path.like("%Mix 1")).one()
    assert ren.genre == "Mixed"


def test_exclude_sweeps_rows_already_under_excluded_path(db_session, two_roots, mocked_mutagen):
    full, copy = two_roots
    covers = str(copy.parent / "covers")
    scan_library(db_session, [LibraryRoot(path=str(full), category="music")], covers)
    assert any("Individual" in p for p in _paths(db_session))
    scan_library(
        db_session,
        [LibraryRoot(path=str(full), category="music", exclude=["Individual"])],
        covers,
    )
    assert not any("Individual" in p for p in _paths(db_session))


def test_no_exclude_is_unchanged(db_session, two_roots, mocked_mutagen):
    full, _ = two_roots
    scan_library(db_session, [LibraryRoot(path=str(full), category="music")], "covers")
    assert len(_paths(db_session)) == 5


def test_set_roots_keeps_exclude_for_surviving_path(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({"library_roots": [
        {"path": "H:/M", "category": "music", "exclude": ["Individual"]},
        {"path": "E:/gone", "category": "music"},
        {"path": "P:/Books", "category": "audiobook_clean"},
    ]}), encoding="utf-8")
    set_library_roots_for_category("music", ["H:/M", "Q:/new"], config_path=str(cfg))
    roots = yaml.safe_load(cfg.read_text(encoding="utf-8"))["library_roots"]
    assert {"path": "H:/M", "category": "music", "exclude": ["Individual"]} in roots
    assert {"path": "Q:/new", "category": "music"} in roots
    assert not [r for r in roots if r["path"] == "E:/gone"]
    assert {"path": "P:/Books", "category": "audiobook_clean"} in roots
