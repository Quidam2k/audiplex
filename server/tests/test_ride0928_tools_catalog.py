"""#ride0928: every registered audiplex-dj tool is in audiplex_mcp/TOOLS.md.

TOOLS.md is the catalog Pantheon's personas read. A tool missing from it is a
feature nobody knows to use, so adding a tool without its line fails here.
"""

import asyncio
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from audiplex_mcp import bucket_tools  # noqa: E402,F401  (registers the bucket tools)
from audiplex_mcp import server as mcp_server  # noqa: E402


def test_every_tool_is_in_the_catalog():
    names = {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}
    catalog = (REPO / "audiplex_mcp" / "TOOLS.md").read_text(encoding="utf-8")
    listed = set(re.findall(r"`(dj_[a-z_]+)`", catalog))
    assert names, "no tools registered"
    missing = sorted(names - listed)
    assert not missing, f"add these to audiplex_mcp/TOOLS.md: {missing}"
    stale = sorted(listed - names)
    assert not stale, f"TOOLS.md lists tools that no longer exist: {stale}"


def test_docstring_points_at_the_catalog():
    assert "TOOLS.md" in (mcp_server.__doc__ or "")
