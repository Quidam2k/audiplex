"""#4114: 10/9 saw a 38 s LLM gap between pool start, skip and resume."""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from audiplex_mcp import server as mcp_server


TRIM = mcp_server.TRIM_OK + " 5 track(s) (command #6)."
ACK = "DONE: skip to the pool's first pick (command #7, device acked ok)."
RESULT = "Stubbed resume result."


@pytest.fixture
def commands(monkeypatch):
    calls = []
    replies = {"ack": ACK, "resume": {"id": 8}}

    async def enqueue(cmd_type, payload):
        calls.append(cmd_type)
        assert payload == {}
        return {"id": 7} if cmd_type == "skip" else replies["resume"]

    async def acked(data, did):
        assert data == {"id": 7}
        assert did == "skip to the pool's first pick"
        return replies["ack"]

    async def result(data, asked):
        assert data == {"id": 8}
        assert asked == 0
        return RESULT

    monkeypatch.setattr(mcp_server, "_enqueue", enqueue)
    monkeypatch.setattr(mcp_server, "_acked", acked)
    monkeypatch.setattr(mcp_server, "_result", result)
    return calls, replies


@pytest.mark.parametrize("trim", [
    " The phone refused the trim: failed x",
    " Sent replace_upcoming (command #5) after the current song;"
    " no ack yet - check dj_command_status.",
])
def test_trim_not_confirmed_sends_nothing(commands, trim):
    calls, _ = commands
    result = asyncio.run(mcp_server._start_pool_now(trim, {"playing": False}))
    assert calls == []
    assert "NO skip or resume was sent" in result


def test_paused_skips_then_resumes(commands):
    calls, _ = commands
    result = asyncio.run(mcp_server._start_pool_now(TRIM, {"playing": False}))
    assert calls == ["skip", "resume"]
    assert "DONE: skip" in result
    assert RESULT in result


def test_already_playing_only_skips(commands):
    calls, _ = commands
    result = asyncio.run(mcp_server._start_pool_now(TRIM, {"playing": True}))
    assert calls == ["skip"]
    assert ACK in result


def test_unconfirmed_skip_does_not_resume(commands):
    calls, replies = commands
    replies["ack"] = "NOT DONE (unconfirmed): no device ack for skip."
    result = asyncio.run(mcp_server._start_pool_now(TRIM, {"playing": False}))
    assert calls == ["skip"]
    assert replies["ack"] in result
    assert "No resume was sent" in result


def test_resume_held_by_talk_guard(commands):
    calls, replies = commands
    held = "Held by talk guard: speech is active."
    replies["resume"] = held
    result = asyncio.run(mcp_server._start_pool_now(TRIM, {"playing": False}))
    assert calls == ["skip", "resume"]
    assert ACK in result
    assert held in result
    assert mcp_server._held_result(held) in result
