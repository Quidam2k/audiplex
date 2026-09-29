"""Find tracks whose DB duration disagrees with the real file (#3249).

Track 986 ("Funeral.. Rebuilding Serenity") says 839 s in the DB; the file is
168 s. A wrong duration misleads the DJ's remaining-time math and outro timing,
and hides a real early stop behind a "complete". This probes each file with
ffprobe and reports mismatches. With --apply it fixes ONLY duration_seconds,
and only after the caller has made a DB backup (--backup-done-at).

  python scripts/audit_track_durations.py                 # report only
  python scripts/audit_track_durations.py --apply --backup-done-at audiplex.db.bak-...
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional

SERVER = Path(__file__).resolve().parents[1]


def probe(ffprobe: str, path: str) -> tuple[Optional[float], str]:
    """(seconds, "") or (None, reason). Never raises."""
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, type(e).__name__
    try:
        return float(out.stdout.strip()), ""
    except ValueError:
        return None, (out.stderr.strip() or "no duration")[:200]


def is_mismatch(db_s: float, real_s: float, tolerance: float) -> bool:
    return abs((db_s or 0.0) - real_s) > max(tolerance, 0.02 * real_s)


def audit(rows: list[tuple], ffprobe: str, tolerance: float, workers: int) -> dict[str, Any]:
    mismatched: list[dict[str, Any]] = []
    missing: list[int] = []
    unprobed: list[dict[str, Any]] = []
    on_disk = []
    for tid, title, fp, db_s in rows:
        if fp and os.path.isfile(fp):
            on_disk.append((tid, title, fp, db_s))
        else:
            missing.append(tid)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        results = list(ex.map(lambda r: probe(ffprobe, r[2]), on_disk))
    for (tid, title, fp, db_s), (real, reason) in zip(on_disk, results):
        if real is None:
            unprobed.append({"id": tid, "reason": reason})
        elif is_mismatch(db_s, real, tolerance):
            mismatched.append({"id": tid, "title": title, "file_path": fp,
                               "db_seconds": db_s, "real_seconds": round(real, 2),
                               "delta": round((db_s or 0.0) - real, 2)})
    mismatched.sort(key=lambda m: abs(m["delta"]), reverse=True)
    return {"checked": len(rows), "mismatched": mismatched,
            "missing": missing, "unprobed": unprobed}


def main(argv: Optional[list[str]] = None) -> int:
    try:
        sys.stdout.reconfigure(errors="replace")  # cp1252 console
    except AttributeError:
        pass
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=str(SERVER / "audiplex.db"))
    ap.add_argument("--ids", default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--tolerance", type=float, default=5.0)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--ffprobe", default="ffprobe")
    ap.add_argument("--report", default="")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--backup-done-at", default="")
    a = ap.parse_args(argv)

    if a.apply and not (a.backup_done_at and Path(a.backup_done_at).is_file()):
        print("--apply needs --backup-done-at <existing DB backup file>", file=sys.stderr)
        return 2

    con = sqlite3.connect(a.db)
    sql = "SELECT id, title, file_path, duration_seconds FROM tracks"
    params: list[Any] = []
    if a.ids:
        ids = [int(x) for x in a.ids.split(",") if x.strip()]
        sql += f" WHERE id IN ({','.join('?' * len(ids))})"
        params = ids
    sql += " ORDER BY id"
    if a.limit:
        sql += f" LIMIT {int(a.limit)}"
    rows = con.execute(sql, params).fetchall()

    report = audit(rows, a.ffprobe, a.tolerance, a.workers)
    report.update(db=a.db, applied=False, backup=a.backup_done_at or None)
    if a.apply and report["mismatched"]:
        with con:
            con.executemany("UPDATE tracks SET duration_seconds=? WHERE id=?",
                            [(m["real_seconds"], m["id"]) for m in report["mismatched"]])
        report["applied"] = True
    con.close()

    out = Path(a.report or SERVER / "data" /
               f"duration-audit-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=1), encoding="utf-8")

    print(f"checked {report['checked']}  mismatched {len(report['mismatched'])}  "
          f"missing {len(report['missing'])}  unprobed {len(report['unprobed'])}  "
          f"applied {report['applied']}")
    for m in report["mismatched"][:20]:
        print(f"{m['id']:>6}  {m['db_seconds']:.0f}->{m['real_seconds']:.0f}  {m['title']}")
    print(f"report: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
