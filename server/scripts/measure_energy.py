"""Score each music track's energy (0-100) with ffmpeg + numpy (#2806).

The DJ uses tracks.energy to order a set (build up, cool down). This fills that
column from a cheap audio analysis: loudness, onset density and brightness
(zero-crossing rate). It is a heuristic ordering signal, not an absolute measure.
Only content_kind='music' rows are touched (filter skipped if the column is absent),
and never a row that already has a value. Tracks Todd has played come first.

Hard rule: it never runs while a ride or live DJ set is going. It checks the bike
DJ state file and the playback API before starting and every --recheck-every files,
stops if busy (keeping what it measured), and sleeps between files. Sequential only.

  python scripts/measure_energy.py --limit 20                  # report only
  python scripts/measure_energy.py --apply --limit 500 --backup-done-at audiplex.db.bak-...
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np

SERVER = Path(__file__).resolve().parents[1]

# Heuristic ordering signal, not an absolute: tune freely, then re-run on a
# scratch copy. Each term is clamped to 0..1 and blended by the weights below.
SR = 11025
FRAME_S = 0.05
SILENCE_DB = -60.0        # frames quieter than this are ignored for loudness
ONSET_RISE_DB = 6.0       # frame-to-frame rise counted as an onset
ONSET_MIN_GAP_S = 0.10
LOUD_FLOOR_DB, LOUD_SPAN_DB = -30.0, 20.0   # -30 dBFS -> 0, -10 dBFS -> 1
ONSET_FULL_PER_S = 4.0
ZCR_FULL = 0.15
W_LOUD, W_ONSET, W_BRIGHT = 0.4, 0.4, 0.2

DEFAULT_BIKE_STATE = "Q:/Pantheon/data/runtime/bike_dj_state.json"
CANT_VERIFY = "can't verify the player is idle"


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def features(samples: np.ndarray, sr: int) -> dict[str, Any]:
    """rms_db, onset_rate (per s), zcr, seconds. rms_db is None if all frames are silent."""
    x = np.asarray(samples, dtype=np.float64)
    seconds = len(x) / sr if sr else 0.0
    n = max(1, int(sr * FRAME_S))
    nf = len(x) // n
    if nf < 1:
        return {"rms_db": None, "onset_rate": 0.0, "zcr": 0.0, "seconds": seconds}
    frames = x[: nf * n].reshape(nf, n)
    db = 20 * np.log10(np.sqrt(np.mean(frames ** 2, axis=1)) + 1e-9)
    audible = db > SILENCE_DB
    rms_db = float(db[audible].mean()) if audible.any() else None
    env = np.maximum(db, SILENCE_DB)
    gap = max(1, int(round(ONSET_MIN_GAP_S / FRAME_S)))
    onsets, last = 0, -gap
    for i in np.flatnonzero(np.diff(env) > ONSET_RISE_DB) + 1:
        if i - last >= gap:
            onsets += 1
            last = i
    signs = x >= 0
    zcr = float(np.mean(signs[1:] != signs[:-1])) if len(x) > 1 else 0.0
    return {"rms_db": rms_db, "onset_rate": onsets / seconds if seconds else 0.0,
            "zcr": zcr, "seconds": seconds}


def energy_score(f: dict[str, Any]) -> int:
    loud = _clamp((f["rms_db"] - LOUD_FLOOR_DB) / LOUD_SPAN_DB)
    onset = _clamp(f["onset_rate"] / ONSET_FULL_PER_S)
    bright = _clamp(f["zcr"] / ZCR_FULL)
    score = round(100 * (W_LOUD * loud + W_ONSET * onset + W_BRIGHT * bright))
    return max(0, min(100, int(score)))


def measure(ffmpeg: str, path: str) -> tuple[Optional[int], Optional[dict], str]:
    """(energy, features, "") or (None, features|None, reason). Never raises."""
    try:
        kw: dict[str, Any] = {}
        flag = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
        if flag:
            kw["creationflags"] = flag
        out = subprocess.run(
            [ffmpeg, "-hide_banner", "-nostats", "-v", "error", "-i", path,
             "-map", "0:a:0", "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"],
            capture_output=True, timeout=600, **kw,
        )
        if out.returncode != 0 and not out.stdout:
            err = out.stderr.decode("utf-8", "replace").strip().splitlines()
            return None, None, (err[-1] if err else "ffmpeg failed")[:200]
        pcm = np.frombuffer(out.stdout[: len(out.stdout) // 2 * 2], dtype="<i2")
        if pcm.size == 0:
            return None, None, "empty decode"
        f = features(pcm.astype(np.float64) / 32768.0, SR)
        if f["rms_db"] is None:
            return None, f, "silent"
        return energy_score(f), f, ""
    except Exception as e:  # never raise: one bad file must not kill a batch
        return None, None, type(e).__name__


def _get_json(url: str, token: str) -> tuple[int, Any]:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"} if token else {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        return e.code, None


def busy_reason(url: str, token: str, bike_state_path: str = DEFAULT_BIKE_STATE) -> str:
    """Empty string when idle, else a sayable reason. Fails closed if the player can't be checked."""
    try:
        p = Path(bike_state_path)
        if p.is_file() and json.loads(p.read_text(encoding="utf-8")).get("active") is True:
            return "a bike ride is active"
    except (OSError, ValueError):
        pass
    base = url.rstrip("/")
    try:
        code, state = _get_json(f"{base}/api/playback/state", token)
        if code != 200 or not isinstance(state, dict):
            return CANT_VERIFY
        if state.get("playing"):
            return "music is playing"
        code, pool = _get_json(f"{base}/api/playback/pool", token)
        if code == 200 and isinstance(pool, dict) and pool.get("active"):
            return "a DJ set is active"
        if code not in (200, 404):
            return CANT_VERIFY
    except Exception:
        return CANT_VERIFY
    return ""


def select_rows(con: sqlite3.Connection, played_only: bool, ids: list[int], limit: int) -> list[tuple]:
    """Music tracks still missing energy; most-played first when played_only."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(tracks)")}
    sql = "SELECT t.id, t.title, t.file_path FROM tracks t"
    where = ["t.energy IS NULL"]
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
        busy: Callable[[], str] = lambda: "", sleeper: Callable[[float], Any] = time.sleep) -> dict[str, Any]:
    measured: list[dict[str, Any]] = []
    missing: list[int] = []
    failed: list[dict[str, Any]] = []
    aborted: Optional[str] = None
    done = 0
    for tid, title, fp in rows:
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
        energy, f, reason = measure(ffmpeg, fp)
        if energy is None:
            failed.append({"id": tid, "reason": reason})
        else:
            measured.append({"id": tid, "title": title, "energy": energy,
                             "rms_db": round(f["rms_db"], 2), "onset_rate": round(f["onset_rate"], 3),
                             "zcr": round(f["zcr"], 4)})
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
    if "energy" not in columns:
        print("tracks.energy is missing: restart the server once to migrate", file=sys.stderr)
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
                "UPDATE tracks SET energy=? WHERE id=? AND energy IS NULL",
                [(m["energy"], m["id"]) for m in report["measured"]],
            )
        report["applied"] = True
    con.close()

    out = Path(a.report or SERVER / "data" /
               f"energy-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=1), encoding="utf-8")

    tail = f"  ABORTED: {report['aborted']}" if report["aborted"] else ""
    print(f"checked {report['checked']}  measured {len(report['measured'])}  "
          f"missing {len(report['missing'])}  failed {len(report['failed'])}  "
          f"applied {report['applied']}{tail}")
    for m in report["measured"][:20]:
        print(f"{m['id']:>6}  {m['energy']:3d}  {m['title']}")
    print(f"report: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
