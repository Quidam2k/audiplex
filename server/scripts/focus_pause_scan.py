"""Scan Audiplex playback diagnostics for audio-focus pauses and classify them.

Read-only. For every "paused by audio_focus" event it decides whether playback
resumed automatically (auto), probably by hand (hand?), ended for another
reason (ended:<source>), or never resumed (stranded). Optionally joins
Pantheon device logs to reclassify "hand?" pauses, and reports which phone
build is running (the "#3249 gain resume" log proves the #3249 build).

A heuristic, not proof: client logs carry the phone clock and state posts the
server clock, and media_session pauses are ignored as echoes of the focus pause.
Read the --list lines against both logs before calling a pause stranded.

Example:
    python server/scripts/focus_pause_scan.py --since 2026-11-22 --list
"""
from __future__ import annotations

import argparse
import bisect
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

FOCUS_MSG = "paused by audio_focus"
RESUME_MARKS = ("focus resume: faded", "gain resume")
SCAN_WINDOW = 1800.0
DEBOUNCE = 3.0
NEAR = 6.0


def load_events(diag_dir: Path) -> list[dict[str, Any]]:
    seen: set[tuple] = set()
    out: list[dict[str, Any]] = []
    names = [f"playback-diag.jsonl.{n}" for n in (3, 2, 1)] + ["playback-diag.jsonl"]  # oldest first
    for name in names:
        path = diag_dir / name
        try:
            fh = path.open(encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict) or isinstance(rec.get("at"), bool)                         or not isinstance(rec.get("at"), (int, float)):
                    continue
                key = (round(rec["at"], 1), str(rec.get("kind")), str(rec.get("event")),
                       str(rec.get("message")), str(rec.get("playing")))
                if key in seen:
                    continue
                seen.add(key)
                out.append(rec)
    out.sort(key=lambda r: r["at"])
    return out


def _msg(e: dict[str, Any]) -> str:
    return str(e.get("message") or "")


def _is_resume_log(e: dict[str, Any]) -> bool:
    return e.get("kind") == "client_log" and any(m in _msg(e) for m in RESUME_MARKS)


def classify(events: list[dict[str, Any]], t0: float, t1: float) -> list[dict[str, Any]]:
    resume_ats = [e["at"] for e in events if _is_resume_log(e)]
    pauses: list[dict[str, Any]] = []
    last_counted: float | None = None
    media = "other"
    for i, e in enumerate(events):
        if e.get("kind") == "state":
            tid = e.get("track_id")
            media = "book" if tid is None else ("music" if isinstance(tid, int) and tid > 0 else "other")
            continue
        if not (e.get("kind") == "client_log" and e.get("event") == "pause_source"
                and _msg(e) == FOCUS_MSG):
            continue
        at = e["at"]
        if last_counted is not None and at - last_counted < DEBOUNCE:
            continue
        last_counted = at
        if not t0 <= at <= t1:
            continue
        cls, delay = "stranded", None
        for f in events[i + 1:]:
            if f["at"] - at > SCAN_WINDOW:
                break
            m = _msg(f)
            is_log = f.get("kind") == "client_log"
            if is_log and _is_resume_log(f):
                cls, delay = "auto", f["at"] - at
                break
            if (f.get("kind") == "state" and f.get("playing") is True) or \
                    (is_log and m.startswith("playWhenReady=true")):
                j = bisect.bisect_left(resume_ats, f["at"] - NEAR)
                near = j < len(resume_ats) and resume_ats[j] <= f["at"] + NEAR
                cls, delay = ("auto" if near else "hand?"), f["at"] - at
                break
            if is_log and f.get("event") == "pause_source" and m != FOCUS_MSG \
                    and "media_session" not in m:
                cls, delay = f"ended:{m.removeprefix('paused by ')}", f["at"] - at
                break
        pauses.append({"at": at, "kind": media, "class": cls, "delay": delay})
    return pauses


def _utc(epoch: float) -> str:
    d = datetime.fromtimestamp(epoch, timezone.utc)
    return d.strftime("%Y-%m-%dT%H:%M:%S.") + f"{d.microsecond // 1000:03d}Z"


def _exists(conn: sqlite3.Connection, like: str, lo: float, hi: float, tag: str = "AudioPlayback",
            exact: bool = False) -> bool:
    op = "=" if exact else "LIKE"
    row = conn.execute(
        f"SELECT 1 FROM device_logs WHERE tag=? AND message {op} ? "
        "AND timestamp BETWEEN ? AND ? LIMIT 1",
        (tag, like, _utc(lo), _utc(hi))).fetchone()
    return row is not None


def join_pantheon(rows: list[dict[str, Any]], conn: sqlite3.Connection) -> list[dict[str, Any]]:
    for r in rows:
        if r["class"] not in ("auto", "hand?", "stranded"):
            continue
        # #3249 the stranding race: this pause IS Talk's late loss (logged ~0.5 s before it)
        if _exists(conn, "talk.focus.reclaim_race", r["at"] - 2, r["at"] + 1, tag="SttStreaming",
                   exact=True):
            r["class"] = "stranded(reclaim_race)"
            continue
        if r["delay"] is None:
            continue
        resume = r["at"] + r["delay"]
        if r["class"] == "hand?" and _exists(conn, "Abandoned audio focus%", resume - 6, resume + 4):
            r["class"] = "auto"  # Pantheon's abandon gave the GAIN; the fade log was cut short
        elif r["class"] == "hand?" and _exists(
                conn, "Burst focus released after grace%", r["at"], resume + 5):
            r["class"] = "hand?(pantheon held)"
    return rows


def _open_pantheon(arg: str | None) -> sqlite3.Connection | None:
    try:
        if arg:
            p = Path(arg)
        else:
            import audiplex_paths
            p = Path(audiplex_paths.root("pantheon_root")) / "data" / "pantheon.db"
        if not p.is_file():
            return None
        return sqlite3.connect(p.resolve().as_uri() + "?mode=ro", uri=True)
    except Exception:
        return None


def _date(s: str) -> str:
    try:
        datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a YYYY-MM-DD date: {s}")
    return s


def _day_bound(s: str, end: bool) -> float:
    d = datetime.strptime(s, "%Y-%m-%d")
    if end:
        d += timedelta(days=1) - timedelta(microseconds=1)
    return d.timestamp()


def _fmt_counts(c: Counter) -> str:
    parts = []
    for kind in ("music", "book", "other"):
        items = sorted((k[1], n) for k, n in c.items() if k[0] == kind and n)
        if items:
            parts.append(f"{kind}: " + ", ".join(f"{cl} {n}" for cl, n in items))
    return " | ".join(parts)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Classify audio-focus pauses.")
    ap.add_argument("--diag", default=str(REPO / "server" / "data"))
    ap.add_argument("--pantheon-db")
    ap.add_argument("--since", type=_date)
    ap.add_argument("--until", type=_date)
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args(argv)

    now = datetime.now()
    t0 = _day_bound(args.since, False) if args.since else (now - timedelta(days=14)).timestamp()
    t1 = _day_bound(args.until, True) if args.until else now.timestamp()
    if t1 < t0:
        ap.error("--until is before --since")

    diag = Path(args.diag)
    events = load_events(diag)
    rows = classify(events, t0, t1)

    conn = _open_pantheon(args.pantheon_db)
    if conn is None:
        print("pantheon db not found: running without the join")
    else:
        try:
            rows = join_pantheon(rows, conn)
        except sqlite3.Error as exc:
            print(f"pantheon join failed: {exc}")
        finally:
            conn.close()

    try:
        installs = json.loads((diag / "app_installs.json").read_text(encoding="utf-8"))
        for dev, info in installs.items():
            since = datetime.fromtimestamp(info["first_seen_at"]).strftime("%Y-%m-%d %H:%M")
            print(f"{dev} on {info['version_name']} since {since}")
    except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError):
        print("app version unknown")
    n3249 = sum(1 for e in events if e.get("kind") == "client_log"
                and t0 <= e["at"] <= t1 and "#3249 gain resume" in _msg(e))
    print(f"#3249 gain resume logs in range: {n3249}"
          + (" (the #3249 build ran in this range)" if n3249 else ""))

    by_day: dict[str, Counter] = {}
    total: Counter = Counter()
    for r in rows:
        day = datetime.fromtimestamp(r["at"]).strftime("%Y-%m-%d")
        key = (r["kind"], r["class"])
        by_day.setdefault(day, Counter())[key] += 1
        total[key] += 1
    for day in sorted(by_day):
        print(f"{day}  {_fmt_counts(by_day[day])}")
    print(f"total  {_fmt_counts(total) or 'no focus pauses'}")

    if args.list:
        for r in rows:
            if r["class"] == "auto":
                continue
            ts = datetime.fromtimestamp(r["at"]).strftime("%m-%d %H:%M:%S")
            d = f"+{r['delay']:.0f}s" if r["delay"] is not None else "n/a"
            print(f"{ts} {r['kind']} {r['class']} {d}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
