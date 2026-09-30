"""Fill each music track's tempo (BPM) and key with ffmpeg + numpy (#1002).

The DJ uses tracks.bpm / musical_key to order a set so each song hands off in a
compatible key near the same tempo (audiplex/harmonic.py). Analysis is in
audiplex/tempo_key.py; this is the throttled batch around it. It decodes a
~2-minute window from the middle of each track, only touches content_kind='music'
rows whose bpm is still empty, and does played tracks first.

Same hard rule as measure_energy.py (it reuses its guard): never while a ride or
live DJ set is going. It checks before starting and every --recheck-every files,
stops if busy (keeping what it measured), fails closed when the player can't be
checked, sleeps between files and runs at below-normal priority. Sequential only.

  python scripts/measure_tempo_key.py --limit 20               # report only
  python scripts/measure_tempo_key.py --apply --limit 500 --backup-done-at audiplex.db.bak-...
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from audiplex.tempo_key import SR, analyze  # noqa: E402
from measure_energy import DEFAULT_BIKE_STATE, busy_reason  # noqa: E402

WINDOW_S = 120.0
FIELDS = ("bpm", "bpm_conf", "beat_offset", "musical_key", "key_conf")


def window(duration: float) -> tuple[float, float]:
    """(start, length) of the analysis window: the middle WINDOW_S of the track."""
    d = float(duration or 0)
    if d <= WINDOW_S or d <= 0:
        return 0.0, WINDOW_S if d <= 0 else d
    return round((d - WINDOW_S) / 2, 2), WINDOW_S


def measure(ffmpeg: str, path: str, duration: float) -> tuple[Optional[dict], str]:
    """(result, "") or (None, reason). Never raises."""
    try:
        start, length = window(duration)
        kw: dict[str, Any] = {}
        flag = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
        if flag:
            kw["creationflags"] = flag
        out = subprocess.run(
            [ffmpeg, "-hide_banner", "-nostats", "-v", "error", "-ss", str(start), "-t", str(length),
             "-i", path, "-map", "0:a:0", "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"],
            capture_output=True, timeout=600, **kw,
        )
        if out.returncode != 0 and not out.stdout:
            err = out.stderr.decode("utf-8", "replace").strip().splitlines()
            return None, (err[-1] if err else "ffmpeg failed")[:200]
        pcm = np.frombuffer(out.stdout[: len(out.stdout) // 2 * 2], dtype="<i2")
        if pcm.size < SR * 10:
            return None, "too short"
        r = analyze(pcm.astype(np.float64) / 32768.0, SR, start_s=start)
        if r["bpm"] is None and r["musical_key"] is None:
            return None, "no beat or key found"
        return r, ""
    except Exception as e:  # never raise: one bad file must not kill a batch
        return None, type(e).__name__


def select_rows(con: sqlite3.Connection, played_only: bool, ids: list[int], limit: int) -> list[tuple]:
    """Music tracks still missing tempo/key; most-played first when played_only."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(tracks)")}
    sql = "SELECT t.id, t.title, t.file_path, t.duration_seconds FROM tracks t"
    where = ["t.bpm IS NULL", "t.musical_key IS NULL"]
    params: list[Any] = []
    if "content_kind" in cols:
        where.append("t.content_kind = 'music'")
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


def run(rows: list[tuple], ffmpeg: str, *, sleep_s: float = 2.0, recheck_every: int = 10,
        busy: Callable[[], str] = lambda: "", sleeper: Callable[[float], Any] = time.sleep,
        measurer: Callable[[str, str, float], tuple[Optional[dict], str]] = measure) -> dict[str, Any]:
    measured: list[dict[str, Any]] = []
    missing: list[int] = []
    failed: list[dict[str, Any]] = []
    aborted: Optional[str] = None
    done = 0
    for tid, title, fp, dur in rows:
        if not (fp and os.path.isfile(fp)):
            missing.append(tid)
            continue
        if done and recheck_every and done % recheck_every == 0:
            aborted = busy() or None
            if aborted:
                break
        if done and sleep_s:
            sleeper(sleep_s)
        done += 1
        r, reason = measurer(ffmpeg, fp, dur)
        if r is None:
            failed.append({"id": tid, "reason": reason})
        else:
            measured.append({"id": tid, "title": title, **{k: r[k] for k in FIELDS},
                             "key_margin": r["key_margin"]})
    return {"checked": len(rows), "measured": measured, "missing": missing,
            "failed": failed, "aborted": aborted}


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
    ap.add_argument("--ffmpeg", default="ffmpeg")
    ap.add_argument("--report", default="")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--backup-done-at", default="")
    ap.add_argument("--url", default=os.environ.get("AUDIPLEX_URL", "http://localhost:8100"))
    ap.add_argument("--token", default=os.environ.get("AUDIPLEX_TOKEN", ""))
    ap.add_argument("--bike-state", default=DEFAULT_BIKE_STATE)
    ap.add_argument("--skip-busy-check", action="store_true",
                    help="isolated test DBs only: NEVER use against the live server")
    ap.add_argument("--sleep", type=float, default=2.0, help="seconds between files")
    ap.add_argument("--recheck-every", type=int, default=10, help="re-check busy every N files")
    a = ap.parse_args(argv)

    if a.apply and not (a.backup_done_at and Path(a.backup_done_at).is_file()):
        print("--apply needs --backup-done-at <existing DB backup file>", file=sys.stderr)
        return 2

    con = sqlite3.connect(a.db)
    columns = {r[1] for r in con.execute("PRAGMA table_info(tracks)")}
    if not set(FIELDS) <= columns:
        print("tracks.bpm/musical_key are missing: restart the server once to migrate", file=sys.stderr)
        return 2

    if a.skip_busy_check:
        def busy() -> str:
            return ""
    else:
        def busy() -> str:
            return busy_reason(a.url, a.token, a.bike_state)
    why = busy()
    if why:
        print(f"not measuring: {why}", file=sys.stderr)
        con.close()
        return 3

    ids = [int(x) for x in a.ids.split(",") if x.strip()]
    rows = select_rows(con, not a.all_tracks, ids, a.limit)

    report = run(rows, a.ffmpeg, sleep_s=a.sleep, recheck_every=a.recheck_every, busy=busy)
    report.update(db=a.db, applied=False, backup=a.backup_done_at or None)
    if a.apply and report["measured"]:
        with con:
            con.executemany(
                "UPDATE tracks SET bpm=?, bpm_conf=?, beat_offset=?, musical_key=?, key_conf=? "
                "WHERE id=? AND bpm IS NULL AND musical_key IS NULL",
                [tuple(m[k] for k in FIELDS) + (m["id"],) for m in report["measured"]],
            )
        report["applied"] = True
    con.close()

    out = Path(a.report or SERVER / "data" /
               f"tempo-key-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=1), encoding="utf-8")

    tail = f"  ABORTED: {report['aborted']}" if report["aborted"] else ""
    print(f"checked {report['checked']}  measured {len(report['measured'])}  "
          f"missing {len(report['missing'])}  failed {len(report['failed'])}  "
          f"applied {report['applied']}{tail}")
    for m in report["measured"][:20]:
        print(f"{m['id']:>6}  {m['bpm'] or '-':>6}  {m['musical_key'] or '-':>3}  {m['title']}")
    print(f"report: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
