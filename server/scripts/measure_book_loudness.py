"""Sample audiobook integrated loudness (EBU R128, LUFS) with ffmpeg (#4222).

Fill books.loudness_lufs so phones can raise books toward persona voices
(~-20 LUFS). Sample narration sequentially; short or unknown durations use
one pass capped at 1800 seconds. Every category is included.

--apply requires an existing DB backup. Each measurement commits immediately;
existing loudness values are never overwritten. The transcription lane guard
pauses sampling if throughput drops below its baseline.

  python scripts/measure_book_loudness.py --limit 20
  python scripts/measure_book_loudness.py --apply --backup-done-at audiplex.db.bak
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

SERVER = Path(__file__).resolve().parents[1]
_INTEGRATED = re.compile(r"Integrated loudness:\s*\n\s*I:\s*(-?\d+(?:\.\d+)?|-inf)\s*LUFS")


def parse_integrated(stderr: str) -> float | None:
    """The summary's integrated loudness, or None (digital silence reads -inf)."""
    matches = _INTEGRATED.findall(stderr)
    if not matches or matches[-1] == "-inf":
        return None
    return float(matches[-1])


def window_starts(duration: float, n: int, window: float) -> list[float]:
    """Evenly spaced starts; [0.0] denotes the short/unknown-duration fallback."""
    if n < 1 or window <= 0:
        raise ValueError("n and window must be positive")
    if not math.isfinite(duration) or duration <= 0 or duration < window * n:
        return [0.0]
    return [max(0.0, min(duration - window, duration * (i + 1) / (n + 1) - window / 2))
            for i in range(n)]


def book_lufs(values: list[float | None]) -> float | None:
    """Rounded median, requiring at least half the attempted windows to parse."""
    good = [v for v in values if v is not None and math.isfinite(v)]
    if not good or len(good) * 2 < len(values):
        return None
    return round(statistics.median(good), 2)


def lane_rate(prev_sample: tuple[float, float],
              cur_sample: tuple[float, float]) -> float | None:
    """Audio seconds per wall second; resets and invalid intervals return None."""
    elapsed = cur_sample[0] - prev_sample[0]
    delta = cur_sample[1] - prev_sample[1]
    if elapsed <= 0 or delta < 0 or not all(map(math.isfinite, (elapsed, delta))):
        return None
    return delta / elapsed


def measure(ffmpeg: str, path: str, start: float, window: float) -> tuple[float | None, str]:
    """(lufs, "") or (None, reason). Never raises ordinary exceptions."""
    try:
        flags = 0
        if os.name == "nt":
            flags = (getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0x4000)
                     | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
        out = subprocess.run(
            [ffmpeg, "-hide_banner", "-nostats", "-ss", str(start),
             "-t", str(window), "-threads", "1", "-i", path,
             "-map", "0:a:0", "-af", "ebur128", "-f", "null", "-"],
            capture_output=True, text=True, errors="replace", timeout=300,
            creationflags=flags,
        )
        lufs = parse_integrated(out.stderr)
        if out.returncode or lufs is None:
            reason = (out.stderr.strip().splitlines() or ["no loudness"])[-1][:200]
            return None, f"ffmpeg exit {out.returncode}: {reason}" if out.returncode else reason
        return lufs, ""
    except Exception as e:
        return None, type(e).__name__


def select_rows(con: sqlite3.Connection, has_column: bool,
                ids: list[int], limit: int) -> list[tuple]:
    """Books still missing loudness; all categories, ordered by id."""
    sql = "SELECT id, title, file_path, duration_seconds FROM books"
    where = ["loudness_lufs IS NULL"] if has_column else []
    params: list[Any] = []
    if ids:
        where.append(f"id IN ({','.join('?' * len(ids))})")
        params.extend(ids)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return con.execute(sql, params).fetchall()


class LaneGuard:
    """Pause while the Whisper lane slows (Jarvis/Karen condition on #7939).

    The lane rewrites status.json only every few minutes, so rates use the
    file's own updated_at and are judged only when it has advanced; a
    wall-clock rate over 60 s would read 0 between writes.
    """

    def __init__(self, args: argparse.Namespace):
        self.a = args
        self.prev = None
        self.baseline = 0.0
        self.paused_s = 0.0
        if not args.no_guard:
            self.prev = self.sample()
            cur = self.wait_advance(args.baseline_s)
            self.baseline = (lane_rate(self.prev, cur) or 0.0) if self.prev and cur else 0.0
            self.prev = cur or self.prev
            print(f"lane {args.lane} baseline {self.baseline:.2f} audio s/s", flush=True)

    def sample(self) -> tuple[float, float] | None:
        """(status updated_at, audio_s_done_run) while the lane runs, else None."""
        try:
            status = json.loads(Path(self.a.lane_status).read_text(encoding="utf-8"))
            lane = status["lanes"][self.a.lane]
            if lane["state"] != "running":
                return None
            sample = (float(lane["updated_at"]), float(lane["audio_s_done_run"]))
            return sample if all(map(math.isfinite, sample)) else None
        except (OSError, ValueError, TypeError, KeyError, UnicodeError):
            return None

    def wait_advance(self, budget_s: float) -> tuple[float, float] | None:
        """The next sample newer than self.prev, polling up to budget_s."""
        waited = 0.0
        while True:
            cur = self.sample()
            if cur and (self.prev is None or cur[0] > self.prev[0]):
                return cur
            if waited >= budget_s:
                return None
            time.sleep(15)
            waited += 15

    def check(self) -> bool:
        """True means the pause budget was exhausted while the lane stayed slow."""
        if self.a.no_guard:
            return False
        cur = self.sample()
        if not cur or not self.prev or cur[0] <= self.prev[0]:
            self.prev = cur or self.prev
            return False  # no new lane write yet (or lane not running): no verdict
        rate = lane_rate(self.prev, cur)
        self.prev = cur
        while rate is not None and self.baseline > 0 and rate < self.a.min_ratio * self.baseline:
            if self.paused_s >= self.a.max_pause_s:
                return True
            print(f"PAUSE lane {self.a.lane}: {rate:.2f} audio s/s; "
                  f"baseline {self.baseline:.2f}", flush=True)
            t0 = time.monotonic()
            cur = self.wait_advance(max(0.0, self.a.max_pause_s - self.paused_s))
            self.paused_s += time.monotonic() - t0
            rate = lane_rate(self.prev, cur) if cur else None
            self.prev = cur or self.prev
        return cur is None and self.paused_s >= self.a.max_pause_s


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(errors="replace")  # cp1252 console
    except AttributeError:
        pass
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=str(SERVER / "audiplex.db"))
    ap.add_argument("--ids", default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--ffmpeg", default="ffmpeg")
    ap.add_argument("--report", default="")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--backup-done-at", default="")
    ap.add_argument("--windows", type=int, default=6)
    ap.add_argument("--window-s", type=float, default=60)
    ap.add_argument("--lane-status", default="Q:/Pantheon/data/audiobook_whisper/status.json")
    ap.add_argument("--lane", default="A")
    ap.add_argument("--baseline-s", type=float, default=900)  # lane writes every ~5 min
    ap.add_argument("--min-ratio", type=float, default=0.7)
    ap.add_argument("--max-pause-s", type=float, default=3600)
    ap.add_argument("--no-guard", action="store_true")
    a = ap.parse_args(argv)
    if a.apply and not (a.backup_done_at and Path(a.backup_done_at).is_file()):
        print("--apply needs --backup-done-at <existing DB backup file>", file=sys.stderr)
        return 2
    durations = (a.window_s, a.baseline_s, a.min_ratio, a.max_pause_s)
    if (a.windows < 1 or a.limit < 0 or not all(map(math.isfinite, durations))
            or min(durations[:3]) <= 0 or a.max_pause_s < 0):
        ap.error("windows, window-s, baseline-s and min-ratio must be positive; limit and max-pause-s nonnegative")
    try:
        ids = [int(x) for x in a.ids.split(",") if x.strip()]
    except ValueError:
        ap.error("--ids must contain comma-separated integers")
    con = sqlite3.connect(a.db)
    columns = {r[1] for r in con.execute("PRAGMA table_info(books)")}
    has_column = "loudness_lufs" in columns
    if not has_column and a.apply:
        with con:
            con.execute("ALTER TABLE books ADD COLUMN loudness_lufs FLOAT")
        has_column = True
    rows = select_rows(con, has_column, ids, a.limit)
    report = dict(checked=0, measured=[], missing=[], failed=[], paused_s=0.0,
                  stopped_reason=None, applied=False, db=a.db, backup=a.backup_done_at or None)
    guard = None
    try:
        guard = LaneGuard(a) if rows else None
        for index, (bid, title, path, duration) in enumerate(rows, 1):
            print(f"{index}/{len(rows)}  {bid}  {title}", flush=True)
            report["checked"] += 1
            if not path or not os.path.isfile(path):
                report["missing"].append(bid)
            else:
                duration = float(duration or 0)
                starts = window_starts(duration, a.windows, a.window_s)
                window = 1800 if duration <= 0 or not math.isfinite(duration) or duration < a.window_s * a.windows else a.window_s
                results = [measure(a.ffmpeg, path, start, window) for start in starts]
                values = [value for value, _ in results]
                lufs = book_lufs(values)
                if lufs is None:
                    reasons = "; ".join(dict.fromkeys(reason for _, reason in results if reason))
                    report["failed"].append({"id": bid, "reason": reasons or "insufficient loudness windows"})
                else:
                    report["measured"].append(dict(id=bid, title=title, lufs=lufs, windows=values))
                    if a.apply:
                        with con:
                            changed = con.execute(
                                "UPDATE books SET loudness_lufs=? WHERE id=? AND loudness_lufs IS NULL",
                                (lufs, bid)).rowcount
                        report["applied"] = report["applied"] or changed > 0
            if guard and guard.check():
                report["stopped_reason"] = "lane_slow"
                break
    except KeyboardInterrupt:
        report["stopped_reason"] = "interrupted"
    finally:
        con.close()
    report["paused_s"] = guard.paused_s if guard else 0.0
    out = Path(a.report or SERVER / "data" /
               f"book-loudness-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(f"checked {report['checked']}  measured {len(report['measured'])}  "
          f"missing {len(report['missing'])}  failed {len(report['failed'])}  "
          f"applied {report['applied']}  paused_s {report['paused_s']:g}  "
          f"stopped_reason {report['stopped_reason']}")
    for m in report["measured"]:
        print(f"{m['id']:>6}  {m['lufs']:6.2f} LUFS  {m['title']}")
    print(f"report: {out}")
    return 130 if report["stopped_reason"] == "interrupted" else 0


if __name__ == "__main__":
    sys.exit(main())
