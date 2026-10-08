"""Themed music bucket store (#5518)."""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from audiplex_mcp import buckets  # noqa: E402


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):  # never touch the live buckets.db
    monkeypatch.setenv("DJ_BUCKETS_DB", str(tmp_path / "b.db"))
    monkeypatch.setattr(bucket_tools, "LUFS_QUEUE", tmp_path / "lufs.txt")  # #7387 never the live queue
    return tmp_path


def test_create_list_get():
    r = buckets.save_bucket("Bravery", "songs about courage", tracks=[3, {"track_id": 7, "path": "a.mp3"}])
    assert r == {"name": "bravery", "created": True, "added": 2, "skipped_duplicates": 0, "track_count": 2}
    (row,) = buckets.list_buckets()
    assert row["name"] == "bravery" and row["track_count"] == 2 and row["origin"] == "planned"
    b = buckets.get_bucket("bravery")
    assert [t["track_id"] for t in b["tracks"]] == [3, 7]
    assert b["tracks"][1]["path"] == "a.mp3"


def test_names_are_case_and_space_insensitive():
    buckets.save_bucket("  Sunset   on the River ", tracks=[1])
    assert buckets.get_bucket("sunset on the river")["name"] == "sunset on the river"
    assert buckets.track_ids("SUNSET ON THE RIVER") == [1]
    with pytest.raises(ValueError):
        buckets.normalize_name("   ")


def test_resave_skips_duplicates_and_appends_in_order():
    buckets.save_bucket("x", tracks=[5, 2])
    r = buckets.save_bucket("x", tracks=[2, 9])
    assert r["created"] is False and r["added"] == 1 and r["skipped_duplicates"] == 1
    assert buckets.track_ids("x") == [5, 2, 9]


def test_replace_resets_tracks_and_origin():
    buckets.save_bucket("x", "old", tracks=[1, 2])
    buckets.save_bucket("x", "new", origin="dj", tracks=[3], replace=True)
    b = buckets.get_bucket("x")
    assert b["description"] == "new" and b["origin"] == "dj"
    assert buckets.track_ids("x") == [3]


def test_add_and_remove():
    buckets.save_bucket("x", tracks=[1])
    assert buckets.add_tracks("x", [2, 1], added_by="jarvis")["track_count"] == 2
    assert buckets.remove_tracks("x", [1, 99]) == {"name": "x", "removed": 1, "track_count": 1}
    assert buckets.track_ids("x") == [2]
    with pytest.raises(ValueError):
        buckets.add_tracks("nope", [1])


def test_find_exact_substring_ambiguous():
    buckets.save_bucket("protest", tracks=[1])
    buckets.save_bucket("protest songs", tracks=[2])
    buckets.save_bucket("sunset on the river", tracks=[3])
    assert buckets.find_bucket("Protest")["name"] == "protest"  # exact beats substring
    assert buckets.find_bucket("sunset")["name"] == "sunset on the river"
    assert buckets.find_bucket("zzz") is None
    buckets.delete_bucket("protest")
    buckets.save_bucket("protest anthems", tracks=[4])
    with pytest.raises(ValueError, match="several"):
        buckets.find_bucket("protest")


def test_bad_inputs():
    with pytest.raises(ValueError):
        buckets.save_bucket("x", origin="robot")
    for bad in (0, -1, "abc", True, 1.5):
        with pytest.raises(ValueError):
            buckets.save_bucket("x", tracks=[bad])


def test_delete_cascades_tracks():
    buckets.save_bucket("x", tracks=[1, 2])
    assert buckets.delete_bucket("x") is True
    assert buckets.delete_bucket("x") is False
    buckets.save_bucket("x")
    assert buckets.track_ids("x") == []
    with pytest.raises(ValueError):
        buckets.track_ids("missing")


def test_connection_closed_after_calls(isolated):
    buckets.save_bucket("x", tracks=[1])
    buckets.list_buckets()
    buckets.find_bucket("x")
    os.replace(isolated / "b.db", isolated / "moved.db")  # PermissionError on Windows if a handle leaked


# --- resolver branch + MCP tools (#5518) ---
import asyncio  # noqa: E402

from audiplex_mcp import bucket_tools, server as mcp_server  # noqa: E402


def run(coro):
    return asyncio.run(coro)


def test_resolver_bucket_kind():
    buckets.save_bucket("bravery", tracks=[{"track_id": 4, "path": "x/y.mp3"}, 9])
    label, tracks = run(mcp_server._resolve_source("bucket", "Brave"))
    assert label == "bucket 'bravery'"
    assert [t["id"] for t in tracks] == [4, 9] and tracks[0]["path"] == "x/y.mp3"
    with pytest.raises(LookupError, match="No bucket"):
        run(mcp_server._resolve_source("bucket", "nothing"))


def test_tools_registered_on_server():
    names = {t.name for t in run(mcp_server.mcp.list_tools())}
    assert {"dj_bucket_list", "dj_bucket_show", "dj_bucket_load", "dj_bucket_save", "dj_bucket_edit"} <= names


def test_load_calls_spec_add_with_bucket_source(monkeypatch):
    calls = []

    async def fake_add(spec, add_sources=None, add_tracks=None):
        calls.append((spec, add_sources))
        return "ok"

    monkeypatch.setitem(bucket_tools._NS, "dj_spec_add", fake_add)
    buckets.save_bucket("sunset on the river", tracks=[1])
    buckets.save_bucket("empty one")
    assert run(bucket_tools.dj_bucket_load("sunset", "ride")) == "ok"
    assert calls == [("ride", [{"kind": "bucket", "query": "sunset on the river", "label": "bucket: sunset on the river"}])]
    assert run(bucket_tools.dj_bucket_load("empty", "ride")).startswith("REFUSED")
    assert "No bucket matching" in run(bucket_tools.dj_bucket_load("zzz", "ride"))


def test_save_from_sources_list_show_edit(monkeypatch):
    async def fake_resolve(kind, query, recursive=True):
        if query == "none":
            raise LookupError("No artist matching 'none'.")
        return "artist 'A'", [{"id": 11, "path": "a/11.mp3"}, {"id": 12}]

    monkeypatch.setitem(bucket_tools._NS, "_resolve_source", fake_resolve)
    out = run(bucket_tools.dj_bucket_save("road songs", "for the river trail", track_ids=[3],
                                          sources=[{"kind": "artist", "query": "A"}], created_by="jarvis",
                                          add_to_set=""))  # #4054 set joining: test_4054_sets
    assert out == "Created bucket 'road songs': +3 tracks, 3 total."
    assert buckets.get_bucket("road songs")["origin"] == "dj"
    assert "ERROR" in run(bucket_tools.dj_bucket_save("x", sources=[{"kind": "artist", "query": "none"}]))
    assert "road songs (3 tracks, dj): for the river trail" in run(bucket_tools.dj_bucket_list())
    assert "11 11.mp3" in run(bucket_tools.dj_bucket_show("road"))
    assert run(bucket_tools.dj_bucket_edit("road", add_track_ids=[20], remove_track_ids=[3])) == \
        "Bucket 'road songs': +1 -1, 3 total."
