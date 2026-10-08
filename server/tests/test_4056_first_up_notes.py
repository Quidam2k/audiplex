"""#4056: a pool that starts the music names its first song with that song's sourced notes."""
import asyncio
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from audiplex_mcp import server as mcp_server  # noqa: E402


def _setup(monkeypatch, tmp_path, notes):
    async def fake_get(path, *a, **k):
        assert path == "/api/music/tracks/7"
        return {"title": "Running Up That Hill", "artist_name": "Kate Bush"}
    monkeypatch.setattr(mcp_server, "_get", fake_get)
    monkeypatch.setenv("DJ_PANTHEON_SRC", str(tmp_path))
    fake = types.SimpleNamespace(rfl_notes=lambda item, max_facts=2: notes,
                                 song=lambda item: f"'{item['title']}' by {item['artist']}")
    monkeypatch.setitem(sys.modules, "song_notes", fake)


def test_first_up_carries_notes(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, [{"kind": "history", "text": "Written in 1985.", "source": "Wikipedia"}])
    out = asyncio.run(mcp_server._first_up_notes(7))
    assert out == (" First up: 'Running Up That Hill' by Kate Bush. SOURCED FACTS (weave in 1, nothing"
                   " beyond these): Written in 1985. (Wikipedia).")


def test_first_up_without_notes_is_just_the_song(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, [])
    assert asyncio.run(mcp_server._first_up_notes(7)) == " First up: 'Running Up That Hill' by Kate Bush."


def test_no_pantheon_or_error_is_silent(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, [])
    monkeypatch.setenv("DJ_PANTHEON_SRC", str(tmp_path / "absent"))
    assert asyncio.run(mcp_server._first_up_notes(7)) == ""

    async def boom(path, *a, **k):
        raise OSError("down")
    monkeypatch.setattr(mcp_server, "_get", boom)
    assert asyncio.run(mcp_server._first_up_notes(7)) == ""


def test_real_pantheon_module_fails_open(monkeypatch, tmp_path):
    """Live Pantheon song_notes, RFL store pointed at nothing: the song, no facts."""
    pantheon_src = Path("Q:/Pantheon/src")
    if not (pantheon_src / "song_notes.py").exists():
        return
    _setup(monkeypatch, tmp_path, [])
    monkeypatch.delitem(sys.modules, "song_notes")
    monkeypatch.setenv("DJ_PANTHEON_SRC", str(pantheon_src))
    monkeypatch.setenv("RFL_NOTES_STORE_PY", str(tmp_path / "missing.py"))
    monkeypatch.syspath_prepend(str(pantheon_src))
    import song_notes
    monkeypatch.setattr(song_notes, "RFL_STORE", tmp_path / "missing.py")
    assert asyncio.run(mcp_server._first_up_notes(7)) == " First up: 'Running Up That Hill' by Kate Bush."
