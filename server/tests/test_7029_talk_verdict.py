"""#7029: Audiplex reads Pantheon's one "is Todd talking" verdict through talk_verdict only."""  # #7029

import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # #7029 repo root
sys.path.insert(0, str(ROOT))
import talk_verdict  # noqa: E402

# One place, one reason each (#7029 J4): files allowed to read the raw speech-state keys.
ALLOWED = {
    "talk_verdict.py": "the one reader (its fallback reads the pre-#7029 keys)",
}
KEY_TUPLE = re.compile(r"""\(\s*["'](stt_active|talk_active)["']\s*,\s*["'](composing|talk_active|stt_active)["']""")  # the pre-#7029 loop form
RAW = re.compile(r"""(\.get\(\s*["'](stt_active|talk_active|composing)["']|\[\s*["'](stt_active|talk_active|composing)["']\s*\](?!\s*=))""")


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z")


def test_key_wins_and_latch_never_counts():
    now = time.time()
    assert talk_verdict.is_talking({"updated_at": iso(now), "stt_active": True,
                                    "todd_talking": {"talking": False, "at": iso(now)}}, now) is False
    assert talk_verdict.is_talking({"updated_at": iso(now),
                                    "todd_talking": {"talking": True, "at": iso(now)}}, now) is True
    assert talk_verdict.is_talking({"updated_at": iso(now), "talk_active": True}, now) is False  # #7028


def test_fallback_and_legacy_fixtures():
    assert talk_verdict.is_talking({"stt_active": True}) is True  # no updated_at: old fixture shape
    assert talk_verdict.is_talking({"composing": True}) is True
    assert talk_verdict.is_talking({"stt_active": False, "talk_active": True}) is False
    assert talk_verdict.is_talking("junk") is False


def test_dead_writer_never_pins_talking():  # J1
    now = time.time()
    old = {"updated_at": iso(now - 6), "stt_active": True,
           "todd_talking": {"talking": True, "at": iso(now - 6)}}
    assert talk_verdict.is_talking(old, now) is False
    stale_key = {"updated_at": iso(now), "todd_talking": {"talking": True, "at": iso(now - 6)}}
    assert talk_verdict.is_talking(stale_key, now) is False


def test_no_other_module_reads_the_raw_keys():  # J4
    assert RAW.search('state.get("stt_active")') and RAW.search('d["talk_active"]')
    assert not RAW.search('d["talk_active"] = 1')
    for old in ('    keys = ("stt_active", "composing") + (("current_claim_holder",) if claim else ())',
                '        "talking": any(data.get(k) for k in ("stt_active", "talk_active", "composing")),'):
        assert KEY_TUPLE.search(old), old  # the two copies this replaced trip the guard
    hits = []
    for base in ("audiplex_mcp", "server/audiplex"):
        for p in (ROOT / base).rglob("*.py"):
            rel = p.relative_to(ROOT).as_posix()
            if "__pycache__" in rel or "/tests/" in rel or rel in ALLOWED:
                continue
            for n, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if RAW.search(line) or KEY_TUPLE.search(line):
                    hits.append(f"{rel}:{n}: {line.strip()[:100]}")
    assert not hits, "read talk_verdict.is_talking() instead (#7029):\n" + "\n".join(hits)


def test_stale_claim_never_holds_a_swap(monkeypatch, tmp_path):  # #7029 Codex J1
    from audiplex_mcp import server as mcp_server
    f = tmp_path / "speech_state.json"
    monkeypatch.setenv("DJ_SPEECH_STATE_FILE", str(f))
    now = time.time()
    f.write_text(json.dumps({"updated_at": iso(now - 6), "current_claim_holder": "claude"}), encoding="utf-8")
    assert mcp_server._todd_talking(claim=True) == "clear"
    f.write_text(json.dumps({"updated_at": iso(now), "current_claim_holder": "claude"}), encoding="utf-8")
    assert mcp_server._todd_talking(claim=True) == "talking"
    assert mcp_server._todd_talking() == "clear"


def test_naive_timestamps_are_utc():
    now = time.time()
    naive = datetime.fromtimestamp(now - 10, timezone.utc).replace(tzinfo=None).isoformat()
    assert talk_verdict.is_talking({"updated_at": naive, "stt_active": True}, now) is False
