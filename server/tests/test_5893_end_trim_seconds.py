"""#5893: set_end_trim.py --seconds sets a chosen cut point (e.g. a movie scene after the song)."""
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import set_end_trim as st  # noqa: E402


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    con = sqlite3.connect(path)
    con.execute("create table tracks (id integer primary key, title text, file_path text, end_trim_seconds real)")
    con.execute("insert into tracks values (1716, 'Shake a Tail Feather', 'x.m4a', null)")
    con.commit()
    con.close()
    # 195.8 s file whose tail is a movie scene, not silence
    monkeypatch.setattr(st, "tail_silence", lambda p: (195.8, None))
    return path


def _run(monkeypatch, db, *args):
    monkeypatch.setattr(sys, "argv", ["set_end_trim.py", "--db", str(db), "--ids", "1716", *args])
    assert st.main() == 0
    return sqlite3.connect(db).execute("select end_trim_seconds from tracks where id=1716").fetchone()[0]


def test_seconds_applies_the_chosen_cut(monkeypatch, db):
    assert _run(monkeypatch, db, "--seconds", "159.9", "--apply") == 159.9


def test_seconds_is_a_dry_run_without_apply(monkeypatch, db):
    assert _run(monkeypatch, db, "--seconds", "159.9") is None


@pytest.mark.parametrize("bad", ["0", "-3", "195.8", "300"])
def test_seconds_outside_the_file_is_left_alone(monkeypatch, db, bad):
    assert _run(monkeypatch, db, "--seconds", bad, "--apply") is None


def test_without_seconds_a_non_silent_tail_is_still_left_alone(monkeypatch, db):
    assert _run(monkeypatch, db, "--apply") is None
