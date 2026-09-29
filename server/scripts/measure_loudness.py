"""Measure each track's integrated loudness (EBU R128, LUFS) with ffmpeg (#3255).

GET /api/playback/history returns tracks.loudness_lufs so the volume-telemetry
report (#3251) can ask "did Todd turn it up because the song was quiet?". This
fills that column. Tracks Todd has actually played come first (--played-only is
the default) because those are the only ones the report can join on.

It writes to the live DB, so --apply needs an existing backup, and it never
touches a track that already has a value. Run it only when nothing is streaming:
ffmpeg reads every file end to end.

  python scripts/measure_loudness.py --limit 20                  # report only
  python scripts/measure_loudness.py --apply --limit 500 --backup-done-at audiplex.db.bak-...
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional

SERVER = Path(__file__).resolve().parents[1]

# ffmpeg's ebur128 filter prints a "Summary:" block to stderr ending with
#   Integrated loudness:
#     I:         -14.2 LUFS
_INTEGRATED = re.compile(r"Integrated loudness:\s*\n\s*I:\s*(-?\d+(?:\.\d+)?|-inf)\s*LUFS")


def parse_integrated(stderr: str) -> Optional[float]:
    """The summary's integrated loudness, or None (digital silence reads -inf)."""
    matches = _INTEGRATED.findall(stderr)
    if not matches or matches[-1] == "-inf":
        return None
    return float(matches[-1])


def measure(ffmpeg: str, path: str) -> tuple[Optional[float], str]:
    """(lufs, "") or (None, reason). Never raises."""
    try:
        out = subprocess.run(
            [ffmpeg, "-hide_banner", "-nostats", "-i", path,
             "-map", "0:a:0", "-af", "ebur128", "-f", "null", "-"],
            capture_output=True, text=True, errors="replace", timeout=600,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, type(e).__name__
    lufs = parse_integrated(out.stderr)
    if lufs is None:
        return None, (out.stderr.strip().splitlines() or ["no loudness"])[-1][:200]
    return lufs, ""


def select_rows(con: sqlite3.Connection, played_only: bool, ids: list[int], limit: int) -> list[tuple]:
    """Tracks still missing a loudness value; most-played first when played_only."""
    sql = "SELECT t.id, t.title, t.file_path FROM tracks t"
    where = ["t.loudness_lufs IS NULL"]
    params: list[Any] = []
    if played_only:
        sql += " JOIN (SELECT track_id, COUNT(*) AS n FROM play_stats GROUP BY track_id) p ON p.track_id = t.id"
    if ids:
        where.append(f"t.id IN ({','.join('?' * len(ids))})")
        params += ids
    sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY p.n DESC, t.id" if played_only else " ORDER BY t.id"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return con.execute(sql, params).fetchall()


def run(rows: list[tuple], ffmpeg: str, workers: int) -> dict[str, Any]:
    measured: list[dict[str, Any]] = []
    missing: list[int] = []
    failed: list[dict[str, Any]] = []
    on_disk = []
    for tid, title, fp in rows:
        if fp and os.path.isfile(fp):
            on_disk.append((tid, title, fp))
        else:
            missing.append(tid)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        results = list(ex.map(lambda r: measure(ffmpeg, r[2]), on_disk))
    for (tid, title, _fp), (lufs, reason) in zip(on_disk, results):
        if lufs is None:
            failed.append({"id": tid, "reason": reason})
        else:
            measured.append({"id": tid, "title": title, "lufs": lufs})
    return {"checked": len(rows), "measured": measured, "missing": missing, "failed": failed}


def main(argv: Optional[list[str]] = None) -> int:
    try:
        sys.stdout.reconfigure(errors="replace")  # cp1252 console
    except AttributeError:
        pass
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=str(SERVER / "audiplex.db"))
    ap.add_argument("--ids", default="")
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--all-tracks", action="store_true", help="not just tracks in play_stats")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--ffmpeg", default="ffmpeg")
    ap.add_argument("--report", default="")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--backup-done-at", default="")
    a = ap.parse_args(argv)

    if a.apply and not (a.backup_done_at and Path(a.backup_done_at).is_file()):
        print("--apply needs --backup-done-at <existing DB backup file>", file=sys.stderr)
        return 2

    con = sqlite3.connect(a.db)
    columns = {r[1] for r in con.execute("PRAGMA table_info(tracks)")}
    if "loudness_lufs" not in columns:
        print("tracks.loudness_lufs is missing: restart the server once to migrate", file=sys.stderr)
        return 2
    ids = [int(x) for x in a.ids.split(",") if x.strip()]
    rows = select_rows(con, not a.all_tracks, ids, a.limit)

    report = run(rows, a.ffmpeg, a.workers)
    report.update(db=a.db, applied=False, backup=a.backup_done_at or None)
    if a.apply and report["measured"]:
        with con:
            con.executemany(
                "UPDATE tracks SET loudness_lufs=? WHERE id=? AND loudness_lufs IS NULL",
                [(m["lufs"], m["id"]) for m in report["measured"]],
            )
        report["applied"] = True
    con.close()

    out = Path(a.report or SERVER / "data" /
               f"loudness-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=1), encoding="utf-8")

    print(f"checked {report['checked']}  measured {len(report['measured'])}  "
          f"missing {len(report['missing'])}  failed {len(report['failed'])}  "
          f"applied {report['applied']}")
    for m in report["measured"][:20]:
        print(f"{m['id']:>6}  {m['lufs']:6.1f} LUFS  {m['title']}")
    print(f"report: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
