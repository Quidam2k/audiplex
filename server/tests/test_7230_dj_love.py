"""#7230 (#3576): dj_love remembers songs Todd loves (no invented stars), dj_loves
lists them, and dj_break_brief shows his stars/loves without private words."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from audiplex_mcp import dj_toolkit, server as mcp  # noqa: E402


def run(coro):
    return asyncio.run(coro)


def test_dj_love_without_stars(monkeypatch):
    posts, puts = [], []
    known = [1, 2]

    async def fake_post(path, body=None):
        posts.append((path, body))
        if path == "/api/playback/tags":
            return {"tracks": known[:], "tags": ["loved"],
                    "added": len(known), "unknown": [999]}
        assert path == "/api/playback/pair-notes"
        return {"id": len(posts)}

    async def fake_put(path, body=None):
        puts.append((path, body))
        raise AssertionError("Love without a number must not assign stars")

    monkeypatch.setattr(mcp, "_post", fake_post)
    monkeypatch.setattr(mcp, "_put", fake_put)
    result = run(dj_toolkit.dj_love(
        [1, 2, 999], "  goes on\n anything   about hope  ", persona="Juno"))
    assert "Loved 2 track(s), no stars" in result
    assert posts == [
        ("/api/playback/tags",
         {"track_ids": [1, 2, 999], "tags": ["loved"], "persona": "Juno"}),
        ("/api/playback/pair-notes",
         {"track_a": 1, "note": "Todd loves this: goes on anything about hope",
          "persona": "Juno"}),
        ("/api/playback/pair-notes",
         {"track_a": 2, "note": "Todd loves this: goes on anything about hope",
          "persona": "Juno"}),
    ]
    known[:] = [1]
    posts.clear()
    assert "Loved 1 track(s), no stars" in run(dj_toolkit.dj_love([1], ""))
    assert posts[-1] == ("/api/playback/pair-notes",
                        {"track_a": 1, "note": "Todd loves this", "persona": None})
    known.clear()
    posts.clear()
    assert run(dj_toolkit.dj_love([999], "hope")).startswith("Nothing saved")
    assert posts == [("/api/playback/tags",
                      {"track_ids": [999], "tags": ["loved"], "persona": None})]
    assert puts == []


def test_dj_love_with_stars(monkeypatch):
    puts, posts = [], []

    async def fake_put(path, body=None):
        puts.append((path, body))
        return {"stars": 5, "stored": 5, "note": "all-time great",
                "unknown": [], "rated": [{"track_id": 1, "rating": 5, "was": None}]}

    async def fake_post(path, body=None):
        posts.append((path, body))
        raise AssertionError("Numbered love must delegate to dj_star")

    monkeypatch.setattr(mcp, "_put", fake_put)
    monkeypatch.setattr(mcp, "_post", fake_post)
    result = run(dj_toolkit.dj_love(
        [1], "all-time great", stars=5, persona="Juno"))
    assert puts == [("/api/playback/ratings",
                     {"track_ids": [1], "stars": 5,
                      "words": "all-time great", "persona": "Juno"})]
    assert "Rated 1 track(s) 5 stars" in result
    assert posts == []


def test_dj_love_private(monkeypatch):
    posts, puts = [], []

    async def fake_post(path, body=None):
        posts.append((path, body))
        if path == "/api/playback/tags":
            return {"tracks": [1], "tags": ["loved"], "added": 1, "unknown": []}
        assert path == "/api/playback/pair-notes"
        return {"id": 1}

    async def fake_put(path, body=None):
        puts.append((path, body))
        raise AssertionError("Private love must not invent stars")

    monkeypatch.setattr(mcp, "_post", fake_post)
    monkeypatch.setattr(mcp, "_put", fake_put)
    result = run(dj_toolkit.dj_love([1], "my sister's song", private=True))
    assert "Loved 1 track(s), no stars" in result
    assert posts == [
        ("/api/playback/tags",
         {"track_ids": [1], "tags": ["loved"], "persona": None}),
        ("/api/playback/pair-notes",
         {"track_a": 1, "note": "Todd loves this: [private] my sister's song",
          "persona": None}),
    ]
    assert puts == []


def test_dj_star_private(monkeypatch):
    puts = []

    async def fake_put(path, body=None):
        puts.append((path, body))
        return {"stars": 5, "stored": 5, "note": body["words"], "unknown": [],
                "rated": [{"track_id": 1, "rating": 5, "was": None}]}

    monkeypatch.setattr(mcp, "_put", fake_put)
    result = run(dj_toolkit.dj_star(
        [1], 5, "my sister's song", persona="Juno", private=True))
    assert puts == [("/api/playback/ratings",
                     {"track_ids": [1], "stars": 5,
                      "words": "[private] my sister's song", "persona": "Juno"})]
    assert "Rated 1 track(s) 5 stars" in result


def test_dj_loves_lists_stars_and_unrated_loves(monkeypatch):
    calls = []
    replies = {
        "/api/playback/ratings": [
            {"track_id": 7, "rating": 4.5, "stars": 4.5,
             "note": "[Juno, said 4.5] his words"}],
        "/api/playback/tags/loved": [
            {"track_id": 7, "title": "Peace", "artist": "Elvis Costello", "energy": 40},
            {"track_id": 9, "title": "Tide", "artist": "Roger Waters", "energy": 30}],
        "/api/music/tracks/7": {"artist_name": "Elvis Costello", "title": "Peace"},
        "/api/playback/pair-notes?track_a=9&limit=20": [
            {"id": 3, "track_a": 9, "track_b": None,
             "note": "Todd loves this: goes on anything about hope", "persona": "Juno"},
            {"id": 2, "track_a": 9, "track_b": None,
             "note": "Todd loves this: older words", "persona": "Juno"}],
    }

    async def fake_get(path):
        calls.append(path)
        assert path in replies
        return replies[path]

    monkeypatch.setattr(mcp, "_get", fake_get)
    result = run(dj_toolkit.dj_loves())
    assert '  [4.5*] 7 | Elvis Costello - Peace  "his words"' in result
    assert "[Juno, said" not in result
    assert "Loved, no number (1):" in result
    assert '  9 | Roger Waters - Tide  "goes on anything about hope"' in result
    assert result.count("7 |") == 1
    assert "older words" not in result
    assert sorted(calls) == sorted(replies)


def test_todd_lines_stars_loves_and_privacy(monkeypatch):
    replies = {
        "/api/playback/ratings": [
            {"track_id": 1, "rating": 5, "stars": 5,
             "note": "[Juno, said 5] all-time great"},
            {"track_id": 2, "rating": 4.5, "stars": 4.5,
             "note": "[Juno, said 4.5] [private] my sister's song"}],
        "/api/playback/tags/loved": [{"track_id": 3}],
        "/api/playback/pair-notes?track_a=3&limit=20": [
            {"id": 1, "track_a": 3, "track_b": None,
             "note": "Todd loves this: hope", "persona": "Juno"}],
    }

    async def fake_get(path):
        assert path in replies
        return replies[path]

    monkeypatch.setattr(mcp, "_get", fake_get)
    unrelated = {"id": 99, "title": None}
    lines = run(dj_toolkit.todd_lines([
        {"id": 1, "title": "Peace"}, {"id": 2, "title": "Quiet"},
        {"id": 3, "title": "Tide"}, unrelated, None,
        {"id": 0, "title": "Invalid"}, {"id": -1, "title": None}]))
    assert lines == [
        "",
        'Todd on Peace: 5 stars (his words: "all-time great"). Colour, not a quote.',
        "Todd on Quiet: 4.5 stars. Colour, not a quote.",
        'Todd on Tide: loves it (his words: "hope"). Colour, not a quote.',
    ]
    assert "private" not in "\n".join(lines)
    assert "my sister" not in "\n".join(lines)
    assert run(dj_toolkit.todd_lines([unrelated])) == []


def test_pair_note_lines_skips_love_notes(monkeypatch):
    calls = []

    async def fake_get(path):
        calls.append(path)
        assert path == "/api/playback/pair-notes?track_a=1&track_b=2&limit=5"
        return [
            {"id": 1, "track_a": 1, "track_b": None,
             "note": "Todd loves this: hope", "persona": "Juno"},
            {"id": 2, "track_a": 1, "track_b": 2,
             "note": "great lift", "persona": None},
        ]

    monkeypatch.setattr(mcp, "_get", fake_get)
    lines = run(mcp._pair_note_lines({"id": 1}, {"id": 2}, None))
    assert lines == ["", "DJ note on this pairing: great lift"]
    assert calls == ["/api/playback/pair-notes?track_a=1&track_b=2&limit=5"]
