"""#5892: asking for music must never restart Todd's audiobook.

Failure 1133 (2026-11-11): a DJ meant to start ride music, dj_now_playing said
"nothing is loaded" while his book sat paused, and dj_resume unpaused the book.
The dj_resume that ran was a raw "resume" in server.py that shadowed the #3601
toolkit tool: FastMCP keeps the FIRST tool registered under a name.
"""

import asyncio
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from audiplex_mcp import dj_toolkit, server as mcp  # noqa: E402

BOOK_STATE = {"playing": False, "track": None, "position_ms": 3_723_000,
              "book": {"id": 7, "title": "King of the Vagabonds"}}
MUSIC_STATE = {"playing": False, "position_ms": 1000, "queue": [],
               "track": {"id": 31508, "title": "Shout", "artist": "Otis Day"}}


def _wire(monkeypatch, state):
    sent = []

    async def fake_get(path):
        if path == "/api/playback/state":
            return state
        if path == "/api/playback/device":
            return {}
        raise AssertionError(f"unexpected GET {path}")

    async def fake_enqueue(cmd, payload):
        sent.append((cmd, payload))
        return {"id": len(sent), "pending": 1}

    async def fake_result(data, asked, wait_ack=True):
        return "RESULT sent=1 "

    class Resp:  # dj_now_playing talks httpx directly
        def __init__(self, body):
            self.status_code, self._body = 200, body

        def json(self):
            return self._body

        def raise_for_status(self):
            pass

    class Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            return Resp(await fake_get(url.split(mcp.AUDIPLEX_URL, 1)[-1]))

    async def no_lines():
        return []

    async def nothing_missing():
        return ""

    monkeypatch.setattr(mcp, "_get", fake_get)
    monkeypatch.setattr(mcp, "_enqueue", fake_enqueue)
    monkeypatch.setattr(mcp, "_result", fake_result)
    monkeypatch.setattr(mcp.httpx, "AsyncClient", Client)
    monkeypatch.setattr(mcp, "_player_error_lines", no_lines)
    monkeypatch.setattr(mcp, "_missing_on_phone", nothing_missing)
    return sent


def test_a_paused_book_is_refused_and_nothing_is_sent(monkeypatch):
    sent = _wire(monkeypatch, BOOK_STATE)
    out = asyncio.run(dj_toolkit.dj_resume())
    assert sent == []
    assert "NOT resumed" in out and "King of the Vagabonds at 1:02:03" in out
    assert "dj_pool_set" in out and "book_ok=True" in out  # Karen: the way back to his book is named


def test_book_ok_resumes_the_book(monkeypatch):
    sent = _wire(monkeypatch, BOOK_STATE)
    out = asyncio.run(dj_toolkit.dj_resume(book_ok=True))
    assert [c for c, _ in sent] == ["resume"]
    assert "the BOOK King of the Vagabonds" in out


def test_paused_music_is_unpaused_and_named(monkeypatch):
    sent = _wire(monkeypatch, MUSIC_STATE)
    out = asyncio.run(dj_toolkit.dj_resume())
    assert [c for c, _ in sent] == ["resume"]
    assert "Otis Day - Shout" in out


def test_already_playing_music_sends_nothing(monkeypatch):
    sent = _wire(monkeypatch, {**MUSIC_STATE, "playing": True})
    out = asyncio.run(dj_toolkit.dj_resume())
    assert sent == [] and "Already playing" in out


def test_unnamed_playback_is_never_replaced_by_a_restore(monkeypatch):
    sent = _wire(monkeypatch, {"playing": True, "track": None, "book": None})
    out = asyncio.run(dj_toolkit.dj_resume())
    assert sent == [] and "NOT restored" in out


def test_now_playing_gives_a_book_precedence_like_dj_resume(monkeypatch):
    _wire(monkeypatch, {**BOOK_STATE, "playing": True, "track": MUSIC_STATE["track"]})
    out = asyncio.run(mcp.dj_now_playing())
    assert out.startswith("Playing BOOK (not music): King of the Vagabonds")


def test_now_playing_names_a_paused_book(monkeypatch):
    _wire(monkeypatch, BOOK_STATE)
    out = asyncio.run(mcp.dj_now_playing())
    assert "Nothing is playing" not in out and "nothing is loaded" not in out
    assert "Paused BOOK (not music): King of the Vagabonds" in out
    assert "dj_pool_set" in out


def test_now_playing_still_reports_music_normally(monkeypatch):
    _wire(monkeypatch, {**MUSIC_STATE, "playing": True, "duration_ms": 200_000})
    out = asyncio.run(mcp.dj_now_playing())
    assert out.startswith("playing: Shout - Otis Day")


# Recorded in a FRESH interpreter: the decorators run once at import, so a test
# process that already imported server.py can no longer see a duplicate.
_RECORD = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
from mcp.server.fastmcp.tools import tool_manager
names = []
orig = tool_manager.ToolManager.add_tool
def add_tool(self, fn, name=None, *a, **k):
    names.append(name or fn.__name__)
    return orig(self, fn, name, *a, **k)
tool_manager.ToolManager.add_tool = add_tool
from audiplex_mcp import server  # noqa: F401  (runs every module's register())
print(json.dumps(names))
"""


def test_no_two_tools_share_a_name():
    out = subprocess.run([sys.executable, "-c", _RECORD, str(REPO)], capture_output=True,
                         text=True, timeout=120, cwd=str(REPO))
    assert out.returncode == 0, out.stderr[-2000:]
    names = json.loads(out.stdout.strip().splitlines()[-1])
    assert len(names) > 50, names
    dupes = sorted({n for n in names if names.count(n) > 1})
    assert not dupes, f"FastMCP keeps the FIRST of these and silently drops the rest: {dupes}"


def test_every_registering_module_is_wired_into_server():
    """Jarvis's condition: the duplicate check must see EVERY module that registers tools."""
    pkg = REPO / "audiplex_mcp"
    server_src = (pkg / "server.py").read_text(encoding="utf-8")
    modules = sorted(p.stem for p in pkg.glob("*.py")
                     if re.search(r"^def register\(mcp", p.read_text(encoding="utf-8"), re.M))
    assert modules, "no registering modules found"
    unwired = [m for m in modules if f"{m}.register(mcp" not in server_src]
    assert not unwired, f"register() never called from server.py: {unwired}"
