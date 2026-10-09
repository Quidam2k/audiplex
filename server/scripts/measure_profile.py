"""Measure EBU R128 track profiles for issues #7335/#3981.
Writes track_audio_profile, track_dips, and tracks.loudness_lufs in SQLite.
Usage: python scripts/measure_profile.py --limit 5 --dry-run
       python scripts/measure_profile.py   (full batch, resumable)
"""

import argparse
import ctypes
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ANALYZER_VERSION = "7335-1"  # Identifies this measurement algorithm.
FRAME_S = 0.1  # ebur128 emits one frame per 100 ms.
WARMUP_S = 0.4  # Discard sentinel values before this time.
SENTINEL_DB = -70.0  # Warmup values at or below this level are missing.
SUSTAIN_S = 1.0  # A state must hold this long to count.
LOUD_MARGIN_DB = 10.0  # Loud means momentary >= integrated minus 10 LU.
SMOOTH_S = 1.0  # Trailing moving average window in seconds.
MEDIAN_S = 10.0  # Centered rolling median window for dip detection.
DIP_DEPTH_DB = 8.0  # Minimum dip depth below the local median.
DIP_MIN_S = 2.0  # Minimum dip duration in seconds.
DEFAULT_DB = Path(__file__).resolve().parents[1] / "audiplex.db"  # Server DB.
DEFAULT_LOG = Path(__file__).resolve().parents[1] / "data" / "measure_profile.log"  # Log.
DEFAULT_SPEECH_STATE = "Q:/Pantheon/data/runtime/speech_state.json"  # Talk state.
DEFAULT_BIKE_STATE = "Q:/Pantheon/data/runtime/bike_dj_state.json"  # Bike DJ state.
BELOW_NORMAL = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0x00004000)  # Priority.

sys.path.insert(0, str(Path(__file__).resolve().parent))
import measure_energy as me
import dip_texture  # #3981

_PTS_RE = re.compile(r"pts_time:([-0-9.einfa]+)")
_VALUE_RE = re.compile(r"lavfi\.r128\.(M|S|I)=(\S+)")


def _number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError, OverflowError):
        return None


def parse_ebur_output(text: str) -> list[dict]:
    frames = []
    current = None
    for line in text.splitlines():
        if line.lstrip().startswith("frame:"):
            match = _PTS_RE.search(line)
            stamp = _number(match.group(1)) if match else None
            current = {
                "t": stamp if stamp is not None else len(frames) * FRAME_S,
                "m": None, "s": None, "i": None,
            }
            frames.append(current)
        if current is not None:
            for match in _VALUE_RE.finditer(line):
                key, value = match.group(1).lower(), _number(match.group(2))
                if key != "i" or value is not None:
                    current[key] = value
    return frames


def profile_from_frames(frames: list[dict]) -> dict:
    result = {
        "duration_s": round(float(frames[-1]["t"]) + FRAME_S, 2) if frames else 0.0,
        "integrated_lufs": None, "intro_quiet_s": None, "outro_fade_s": None,
        "dips": [], "analyzer_version": ANALYZER_VERSION,
    }
    if not frames:
        return result
    t = np.asarray([f["t"] for f in frames], dtype=float)
    m = np.asarray([f.get("m") for f in frames], dtype=float)
    m[~np.isfinite(m) | ((t < WARMUP_S) & (m <= SENTINEL_DB))] = np.nan
    valid = np.flatnonzero(~np.isnan(m))
    if not valid.size:
        return result
    indices = np.maximum.accumulate(np.where(np.isnan(m), -1, np.arange(m.size)))
    indices[indices < 0] = valid[0]
    m = m[indices]
    integrated = None
    for frame in frames:
        value = _number(frame.get("i"))
        if value is not None:
            integrated = value
    if integrated is None:
        return result
    result["integrated_lufs"] = round(integrated, 2)
    window = max(1, round(SMOOTH_S / FRAME_S))
    smooth = np.convolve(  # #3981 centered, so the smoother adds no lag to intro/outro
        np.pad(m, (window // 2, (window - 1) // 2), mode="edge"),
        np.ones(window) / window, mode="valid",
    )
    loud = smooth >= integrated - LOUD_MARGIN_DB
    n = max(1, round(SUSTAIN_S / FRAME_S))
    sustained = (
        np.flatnonzero(np.convolve(loud.astype(int), np.ones(n, dtype=int), "valid") == n)
        if loud.size >= n else np.array([], dtype=int)
    )
    intro = None
    outro_run_start = None
    if sustained.size:
        first, last = int(sustained[0]), int(sustained[-1])
        intro = 0.0 if first == 0 else float(t[first])
        j = last + n - 1
        outro_run_start = float(t[j - n + 1])
        result["intro_quiet_s"] = round(intro, 2)
        result["outro_fade_s"] = round(
            max(0.0, float(t[-1]) + FRAME_S - (float(t[j]) + FRAME_S)), 2
        )  # #3981 loud run ends at t[j]+FRAME_S
    window = max(1, round(MEDIAN_S / FRAME_S))
    padded = np.pad(smooth, (window // 2, (window - 1) // 2), mode="edge")
    median = np.median(np.lib.stride_tricks.sliding_window_view(padded, window), axis=1)
    depth = median - smooth
    dipped = smooth <= median - DIP_DEPTH_DB
    transitions = np.diff(np.r_[False, dipped, False].astype(int))
    for start, stop in zip(np.flatnonzero(transitions == 1), np.flatnonzero(transitions == -1)):
        if (stop - start) * FRAME_S < DIP_MIN_S:
            continue
        start_s = float(t[start])
        kind = "mid"
        if start_s < (intro if intro is not None else 0.0) + 0.5:
            kind = "intro"
        elif outro_run_start is not None and start_s >= outro_run_start:
            kind = "outro"
        result["dips"].append({
            "start_s": round(start_s, 2),
            "end_s": round(float(t[stop - 1]) + FRAME_S, 2),
            "depth_db": round(float(np.max(depth[start:stop])), 2), "kind": kind,
        })
    result["dips"].sort(key=lambda dip: dip["start_s"])
    return result


def analyze_file(path: str, ffmpeg: str = "ffmpeg") -> dict:
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-nostats", "-threads", "1", "-i", path,
         "-map", "0:a:0", "-af", "ebur128=metadata=1,ametadata=mode=print:file=-",
         "-f", "null", "-"],
        capture_output=True, timeout=1800,
        creationflags=BELOW_NORMAL if os.name == "nt" else 0,  # #3981 POSIX rejects it
    )
    frames = parse_ebur_output(proc.stdout.decode("utf-8", errors="replace"))
    if proc.returncode and not frames:
        lines = proc.stderr.decode("utf-8", errors="replace").strip().splitlines()
        raise ValueError(lines[-1] if lines else f"ffmpeg exited with code {proc.returncode}")
    return {**profile_from_frames(frames), "path": path}


def ensure_tables(con):
    con.executescript("""
        CREATE TABLE IF NOT EXISTS track_audio_profile (
            track_id INTEGER PRIMARY KEY REFERENCES tracks(id),
            integrated_lufs REAL, intro_quiet_s REAL, outro_fade_s REAL,
            duration_s REAL, analyzed_at TEXT, analyzer_version TEXT
        );
        CREATE TABLE IF NOT EXISTS track_dips (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            track_id INTEGER NOT NULL REFERENCES tracks(id),
            start_s REAL, end_s REAL, depth_db REAL, kind TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_track_dips_track ON track_dips (track_id);
    """)
    columns = {row[1] for row in con.execute("PRAGMA table_info(track_dips)")}  # #3981
    for name, kind in (("texture", "TEXT"), ("texture_conf", "REAL")):  # #3981
        if name not in columns:  # #3981
            con.execute(f"ALTER TABLE track_dips ADD COLUMN {name} {kind}")  # #3981


def store(con, track_id, profile):
    with con:
        con.execute("""
            INSERT OR REPLACE INTO track_audio_profile
            (track_id, integrated_lufs, intro_quiet_s, outro_fade_s,
             duration_s, analyzed_at, analyzer_version) VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (track_id, profile["integrated_lufs"], profile["intro_quiet_s"],
              profile["outro_fade_s"], profile["duration_s"],
              datetime.now(timezone.utc).isoformat(timespec="seconds"),
              profile["analyzer_version"]))
        con.execute("DELETE FROM track_dips WHERE track_id=?", (track_id,))
        con.executemany("""
            INSERT INTO track_dips (track_id, start_s, end_s, depth_db, kind, texture, texture_conf)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, [(track_id, d["start_s"], d["end_s"], d["depth_db"], d["kind"],
               d.get("texture"), d.get("texture_conf"))  # #3981
              for d in profile["dips"]])
        if profile["integrated_lufs"] is not None:
            con.execute("UPDATE tracks SET loudness_lufs=? WHERE id=?",
                        (profile["integrated_lufs"], track_id))


def select_rows(con, ids: list[int], limit: int) -> list[tuple]:
    ensure_tables(con)
    columns = {row[1] for row in con.execute("PRAGMA table_info(tracks)")}
    sql = """
        SELECT t.id, t.title, t.file_path FROM tracks AS t
        LEFT JOIN (SELECT track_id, COUNT(*) AS n FROM play_stats GROUP BY track_id)
            AS p ON p.track_id = t.id
        WHERE NOT EXISTS (
            SELECT 1 FROM track_audio_profile AS a
            WHERE a.track_id = t.id AND a.analyzer_version = ?
        )
    """
    params = [ANALYZER_VERSION]
    if "content_kind" in columns:
        sql += " AND t.content_kind = ?"
        params.append("music")
    if ids:
        sql += " AND t.id IN (" + ",".join("?" for _ in ids) + ")"
        params.extend(ids)
    sql += " ORDER BY COALESCE(p.n, 0) DESC, t.id"
    if limit > 0:
        sql += " LIMIT ?"
        params.append(limit)
    return con.execute(sql, params).fetchall()


def talk_busy(speech_state_path: str) -> str:
    for attempt in range(2):
        try:
            with open(speech_state_path, encoding="utf-8") as handle:
                state = json.load(handle)
            if not isinstance(state, dict):
                raise ValueError("speech state is not a dict")
            return "Todd has Talk on in Pantheon" if state.get("talk_active") else ""
        except FileNotFoundError:
            return ""
        except (OSError, ValueError):
            if attempt == 0:
                time.sleep(0.05)
    return "can't read Pantheon speech state"


def texture_of(path, dip, ffmpeg) -> tuple:
    """#3981: (texture, conf) for one dip window, (None, None) if it can't be decoded."""
    try:
        out = dip_texture.label_window(
            dip_texture.decode_window(path, dip["start_s"], dip["end_s"], ffmpeg))
        return out["texture"], out["texture_conf"]
    except Exception:
        return None, None


def wait_idle(busy, log, sleeper, poll_s, now) -> float:
    """Block while busy() names a reason; returns seconds waited."""
    waited, previous = 0.0, None
    while reason := busy():
        if reason != previous:
            log(f"waiting: {reason}")
            previous = reason
        start = now()
        sleeper(poll_s)
        waited += max(0.0, now() - start)
    return waited


def backfill_textures(con, *, ffmpeg, poll_s, busy, log, sleeper=time.sleep,
                      now=time.monotonic) -> dict:
    """#3981: label dips stored before dip_texture existed (texture IS NULL)."""
    ensure_tables(con)
    rows = con.execute("""
        SELECT d.id, d.start_s, d.end_s, t.file_path FROM track_dips AS d
        JOIN tracks AS t ON t.id = d.track_id
        WHERE d.texture IS NULL ORDER BY d.track_id, d.start_s
    """).fetchall()
    summary = dict(dips=len(rows), labeled=0, failed=0, waited_s=0.0)
    for dip_id, start_s, end_s, path in rows:
        summary["waited_s"] += wait_idle(busy, log, sleeper, poll_s, now)
        texture, conf = (texture_of(path, {"start_s": start_s, "end_s": end_s}, ffmpeg)
                         if path and Path(path).is_file() else (None, None))
        if texture is None:
            summary["failed"] += 1
            continue
        with con:
            con.execute("UPDATE track_dips SET texture=?, texture_conf=? WHERE id=?",
                        (texture, conf, dip_id))
        summary["labeled"] += 1
    summary["waited_s"] = round(summary["waited_s"], 2)
    return summary


def run_batch(rows, *, con, ffmpeg, dry_run, sleep_ratio, poll_s, busy, log,
              sleeper=time.sleep, now=time.monotonic) -> dict:
    summary = dict(checked=0, analyzed=0, skipped_missing=0, failed=0, waited_s=0.0)
    for track_id, title, path in rows:
        summary["checked"] += 1
        summary["waited_s"] += wait_idle(busy, log, sleeper, poll_s, now)
        if not path or not Path(path).is_file():
            log(f"missing file: {track_id} {path}")
            summary["skipped_missing"] += 1
            continue
        try:
            start = now()
            profile = analyze_file(path, ffmpeg)
            for dip in profile["dips"]:  # #3981
                dip["texture"], dip["texture_conf"] = texture_of(path, dip, ffmpeg)
            elapsed = max(0.0, now() - start)
            log(f"ok id={track_id} title={title} I={profile['integrated_lufs']} LU "
                f"intro={profile['intro_quiet_s']} outro={profile['outro_fade_s']} "
                f"dips={len(profile['dips'])} wall={elapsed:.2f}s")
            if not dry_run:
                store(con, track_id, profile)
        except Exception as exc:
            log(f"failed: {track_id} {type(exc).__name__}: {exc}")
            summary["failed"] += 1
            continue
        summary["analyzed"] += 1
        if sleep_ratio > 0:
            sleeper(sleep_ratio * elapsed)
    summary["waited_s"] = round(summary["waited_s"], 2)
    return summary


def main(argv=None) -> int:
    sys.stdout.reconfigure(errors="replace")
    if os.name == "nt":
        try:
            ctypes.windll.kernel32.SetPriorityClass(
                ctypes.windll.kernel32.GetCurrentProcess(), BELOW_NORMAL)
        except Exception:
            pass
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--ids", "--track-id", default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--log", default=str(DEFAULT_LOG))
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--sleep-ratio", type=float, default=1.0)
    parser.add_argument("--poll-s", type=float, default=30.0)
    parser.add_argument("--url", default=os.environ.get("AUDIPLEX_URL", "http://localhost:8100"))
    parser.add_argument("--token", default=os.environ.get("AUDIPLEX_TOKEN"))
    parser.add_argument("--bike-state", default=DEFAULT_BIKE_STATE)
    parser.add_argument("--speech-state", default=DEFAULT_SPEECH_STATE)
    parser.add_argument("--textures-only", action="store_true",
                        help="#3981: only label stored dips that have no texture yet")
    parser.add_argument("--watch", action="store_true",
                        help="#3981: never exit; re-check for new tracks and unlabeled dips")
    parser.add_argument("--watch-s", type=float, default=900.0)
    parser.add_argument("--skip-busy-check", action="store_true",
                        help="tests only, never use against the live server")
    args = parser.parse_args(argv)
    if not args.token:  # #3981: no token = every busy check 401s and the batch waits forever
        try:
            args.token = (Path(__file__).resolve().parents[2] / ".dj_token").read_text(encoding="utf-8").strip()
        except OSError:
            args.token = ""
    try:
        ids = [int(part.strip()) for part in args.ids.split(",") if part.strip()]
    except ValueError:
        parser.error("--ids/--track-id must contain comma-separated integers")
    if not math.isfinite(args.poll_s) or args.poll_s <= 0:
        parser.error("--poll-s must be finite and positive")
    if not math.isfinite(args.sleep_ratio):
        parser.error("--sleep-ratio must be finite")
    with closing(sqlite3.connect(args.db)) as con:
        con.execute("PRAGMA foreign_keys=OFF")
        if "loudness_lufs" not in {r[1] for r in con.execute("PRAGMA table_info(tracks)")}:
            print("restart the server once to migrate", file=sys.stderr)
            return 2
        log_path = Path(args.log)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as handle:
            def log(message):
                print(message, flush=True)
                handle.write(message + "\n")
                handle.flush()

            def busy():
                if args.skip_busy_check:
                    return ""
                try:
                    reason = me.busy_reason(args.url, args.token, args.bike_state)
                except Exception:
                    return me.CANT_VERIFY
                return reason or talk_busy(args.speech_state)

            while True:  # #3981 --watch: picks up new ingests
                if not args.textures_only:
                    summary = run_batch(
                        select_rows(con, ids, args.limit), con=con, ffmpeg=args.ffmpeg,
                        dry_run=args.dry_run, sleep_ratio=args.sleep_ratio,
                        poll_s=args.poll_s, busy=busy, log=log)
                    log("summary: " + json.dumps(summary, sort_keys=True))
                if not args.dry_run and (args.textures_only or args.watch):
                    summary = backfill_textures(con, ffmpeg=args.ffmpeg, poll_s=args.poll_s,
                                                busy=busy, log=log)
                    log("textures: " + json.dumps(summary, sort_keys=True))
                if not args.watch:
                    return 0
                time.sleep(args.watch_s)


if __name__ == "__main__":
    raise SystemExit(main())
