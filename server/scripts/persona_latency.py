"""#5648 persona-line latency gate: queue -> play_start, p50/p95, read-only.

Jarvis's condition on the ahead-of-time DJ render (asg 11715): measure persona-line
latency before and after the prepare render ships; if p95 grows by more than 1 s,
stop and report. This is the measurement, kept in the repo so before and after use
the same query.

    py -3.11 server/scripts/persona_latency.py --days 7
    py -3.11 server/scripts/persona_latency.py --since 2026-11-12T00:00:00Z

Latency = speech_queue.timestamp (queued) to the first audio_events 'play_start'
for that speech id. It includes speech-gate holds and Todd-talking waits, so it is
noisy; compare like windows (same hours, similar traffic).
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

_REPO = str(Path(__file__).resolve().parents[2])  # #5648 audiplex_paths lives at the repo root
if _REPO not in sys.path:
    sys.path.append(_REPO)
import audiplex_paths  # noqa: E402

MAX_S = 600  # a line held longer than 10 min is a parked line, not latency


def _pct(xs: list[float], f: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(f * len(xs)))]


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.rstrip("Z"))


def latencies(db: sqlite3.Connection, since: str) -> dict[str, list[float]]:
    rows = db.execute(
        """SELECT q.agent, q.timestamp, MIN(e.timestamp)
           FROM speech_queue q
           JOIN audio_events e ON e.speech_id = q.id AND e.event_type = 'play_start'
           WHERE q.timestamp >= ?
           GROUP BY q.id""",
        (since,),
    ).fetchall()
    by: dict[str, list[float]] = {}
    for agent, queued, started in rows:
        d = (_ts(started) - _ts(queued)).total_seconds()
        if 0 <= d < MAX_S:
            by.setdefault(agent, []).append(d)
    return by


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=audiplex_paths.pantheon("data", "pantheon.db"))
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--since", help="ISO-8601 UTC start, overrides --days")
    a = ap.parse_args(argv)
    db = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    since = a.since or db.execute(
        "SELECT strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?)", (f"-{a.days} days",)
    ).fetchone()[0]
    by = latencies(db, since)
    allx = [d for xs in by.values() for d in xs]
    if not allx:
        print(f"since={since} n=0")
        return 1
    print(f"since={since} n={len(allx)} p50={_pct(allx, .5):.2f}s p95={_pct(allx, .95):.2f}s")
    for agent, xs in sorted(by.items(), key=lambda kv: -len(kv[1])):
        print(f"  {agent:12s} n={len(xs):5d} p50={_pct(xs, .5):6.2f} p95={_pct(xs, .95):6.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
