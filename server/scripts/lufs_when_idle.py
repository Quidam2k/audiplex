"""#7387: run measure_loudness.py over the library only while nothing streams.

measure_loudness.py reads every file end to end, so it must not compete with a
live stream. This loops small batches and, before each one, checks the phone's
last reported state in data/playback-diag.jsonl: playing, or a report newer than
IDLE_S, means wait. Priority ids (the ride-set lanes + played set) go first, then
the rest of the library. Backs up the DB once (sqlite backup API, safe while the
server runs) before the first write.

  python scripts/lufs_when_idle.py --priority-file data/lufs_priority_ids.txt
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER / "scripts"))
import measure_loudness  # noqa: E402

DIAG = SERVER / "data" / "playback-diag.jsonl"
IDLE_S = 600  # no playing report for 10 min


def streaming(now: float) -> bool:
    """True if the phone reported playing, or reported anything, within IDLE_S."""
    try:
        with DIAG.open("rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 20000))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return False
    for line in reversed(lines):
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("kind") != "state":
            continue
        return bool(ev.get("playing")) or now - float(ev.get("at", 0)) < IDLE_S
    return False


def null_ids(db: str, ids: list[int]) -> list[int]:
    con = sqlite3.connect(db)
    try:
        sql = "SELECT id FROM tracks WHERE loudness_lufs IS NULL"
        if ids:
            sql += f" AND id IN ({','.join(map(str, ids))})"
        return [r[0] for r in con.execute(sql + " ORDER BY id")]
    finally:
        con.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(SERVER / "audiplex.db"))
    ap.add_argument("--priority-file", default="")
    ap.add_argument("--batch", type=int, default=100)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--poll-s", type=int, default=120)
    a = ap.parse_args()

    backup = Path(f"{a.db}.bak-7387-{time.strftime('%Y%m%dT%H%M%S')}")
    src, dst = sqlite3.connect(a.db), sqlite3.connect(str(backup))
    with dst:
        src.backup(dst)
    src.close(); dst.close()
    print(f"backup: {backup}", flush=True)

    prio = [int(x) for x in Path(a.priority_file).read_text().split(",") if x.strip()] if a.priority_file else []
    phases = ([("priority", prio)] if prio else []) + [("library", [])]
    for name, ids in phases:
        todo = null_ids(a.db, ids)  # each id is tried once; failures stay null
        print(f"{name}: {len(todo)} to measure", flush=True)
        while todo:
            if streaming(time.time()):
                print(f"[{time.strftime('%H:%M:%S')}] streaming, waiting", flush=True)
                time.sleep(a.poll_s)
                continue
            chunk, todo = todo[:a.batch], todo[a.batch:]
            measure_loudness.main(["--db", a.db, "--all-tracks", "--limit", "0", "--workers", str(a.workers),
                                   "--ids", ",".join(map(str, chunk)), "--apply", "--backup-done-at", str(backup),
                                   "--report", str(SERVER / "data" / "loudness-7387-last.json")])
        print(f"{name} done: {len(null_ids(a.db, ids))} still null", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
