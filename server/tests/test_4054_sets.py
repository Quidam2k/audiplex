"""#4054: DJ sets are first-class. A one-source pool can't silently replace the ride
set, and a new bucket joins todd-ride-mix as a lane without cutting the queue."""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from audiplex_mcp import bucket_tools, buckets  # noqa: E402
from audiplex_mcp import server as mcp_server  # noqa: E402

RIDE = {"id": 2, "name": "todd-ride-mix", "sources": [
    {"kind": "folder_match", "query": "faster"}, {"kind": "folder_match", "query": "slower"}]}


@pytest.fixture
def specs(monkeypatch):
    calls = []

    async def fake_get(path, *a, **k):
        calls.append(("GET", path))
        if path == "/api/playback/mix-specs":
            return [RIDE]
        raise AssertionError(f"unexpected GET {path}")

    monkeypatch.setattr(mcp_server, "_get", fake_get)
    return calls


def test_single_inline_source_refused_while_a_set_exists(specs):
    out = asyncio.run(mcp_server.dj_pool_set(sources=[{"kind": "bucket", "query": "whole albums"}]))
    assert out.startswith("REFUSED")
    assert "todd-ride-mix" in out and "replace_set=True" in out  # names the override: no retry loop
    assert ("GET", "/api/playback/mix-specs") in specs


def test_replace_set_without_a_reason_still_refused(specs):
    out = asyncio.run(mcp_server.dj_pool_set(sources=[{"kind": "bucket", "query": "x"}], replace_set=True))
    assert out.startswith("REFUSED")


@pytest.fixture
def bucket_env(monkeypatch, tmp_path):
    monkeypatch.setenv("DJ_BUCKETS_DB", str(tmp_path / "b.db"))
    monkeypatch.setattr(bucket_tools, "LUFS_QUEUE", tmp_path / "lufs.txt")
    added = []

    async def get_spec(name):
        return dict(RIDE, sources=RIDE["sources"] + [s for _, s in added])

    async def spec_add(spec, add_sources, replan=True):
        assert replan is False  # #4054: only append the lane, never re-pick the queue
        added.extend((spec, s) for s in add_sources)
        return f"Added to spec '{spec}'"

    monkeypatch.setattr(bucket_tools, "_NS", {"_get_spec": get_spec, "dj_spec_add": spec_add})
    return added, tmp_path


def test_new_bucket_lands_in_the_ride_set(bucket_env):
    added, tmp = bucket_env
    out = asyncio.run(bucket_tools.dj_bucket_save("whole albums", track_ids=[11, 12]))
    assert added == [("todd-ride-mix", {"kind": "bucket", "query": "whole albums",
                                        "label": "bucket: whole albums"})]
    assert "Added to spec 'todd-ride-mix'" in out
    assert (tmp / "lufs.txt").read_text() == "11,12"  # #7387 measured before it plays
    # saving again doesn't add a second lane
    out = asyncio.run(bucket_tools.dj_bucket_save("whole albums", track_ids=[13]))
    assert len(added) == 1 and "Already a lane" in out


def test_add_to_set_blank_opts_out(bucket_env):
    added, _ = bucket_env
    asyncio.run(bucket_tools.dj_bucket_save("solo", track_ids=[1], add_to_set=""))
    assert added == []
    assert buckets.track_ids("solo") == [1]
