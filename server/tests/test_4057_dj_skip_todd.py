"""#4057: dj_skip marks a skip Todd asked for, so ride learning counts it as his."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from audiplex_mcp import server as mcp_server  # noqa: E402


def test_dj_skip_marks_todd_asked(monkeypatch):
    sent = []

    async def fake_enqueue(type_, payload):
        sent.append((type_, payload))
        return {"id": 1}

    async def fake_acked(data, label):
        return "ok"

    monkeypatch.setattr(mcp_server, "_enqueue", fake_enqueue)
    monkeypatch.setattr(mcp_server, "_acked", fake_acked)
    asyncio.run(mcp_server.dj_skip())
    asyncio.run(mcp_server.dj_skip(todd_asked=True))
    assert sent == [("skip", {}), ("skip", {"by": "todd"})]
