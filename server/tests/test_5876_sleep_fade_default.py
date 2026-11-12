"""#5876: every sleep entry point fades over the app's 5 s, voice-started ones included.

The MCP always sends fade_seconds, so its default (was 120) overrode the app's
SleepFade.DEFAULT_FADE_SECONDS on every "Jarvis, sleep mode".
"""

import asyncio
import inspect
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from audiplex_mcp import server as mcp_server  # noqa: E402

SLEEP_FADE_KT = ROOT / "android/app/src/main/java/com/audiplex/app/playback/SleepFade.kt"


def _kotlin_fade_seconds() -> int:
    m = re.search(r"const val HANDOFF_FADE_SECONDS = (\d+)", SLEEP_FADE_KT.read_text(encoding="utf-8"))
    assert m, "SleepFade.HANDOFF_FADE_SECONDS not found"
    return int(m.group(1))


def test_mcp_default_matches_the_app():
    assert mcp_server.SLEEP_FADE_SECONDS == _kotlin_fade_seconds() == 5


def test_both_tools_default_to_it():
    for fn in (mcp_server.dj_sleep_timer, mcp_server.dj_sleep_start):
        fn = getattr(fn, "fn", fn)
        assert inspect.signature(fn).parameters["fade_seconds"].default == mcp_server.SLEEP_FADE_SECONDS


def test_no_arg_sleep_timer_sends_5(monkeypatch):
    sent = []

    async def fake_enqueue(cmd, payload):
        sent.append((cmd, payload))
        return {"id": 1}

    async def acked(cid, wait):
        return {"ack_status": "ok"}

    monkeypatch.setattr(mcp_server, "_enqueue", fake_enqueue)
    monkeypatch.setattr(mcp_server, "_ack_row", acked)
    fn = getattr(mcp_server.dj_sleep_timer, "fn", mcp_server.dj_sleep_timer)
    out = asyncio.run(fn(30))
    assert sent == [("sleep_timer", {"minutes": 30, "fade_seconds": 5})]
    assert "last 5s of 30 min" in out


def test_server_fallback_route_defaults_to_5():
    src = (ROOT / "server/audiplex/routers/playback.py").read_text(encoding="utf-8")
    assert 'body.get("fade_seconds", 5)' in src
    assert 'body.get("fade_seconds", 120)' not in src
