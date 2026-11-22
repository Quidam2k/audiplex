"""#6710: read-only scan of the music library for long silent endings and for
silence followed by more sound (a hidden track, or a dead gap mid-file).

Never edits the DB or any audio. Results append to a jsonl file, one line per
track, so a stopped scan resumes where it left off. Heavy: one ffmpeg at a time,
below-normal priority, and it waits (between files) until the machine has been
idle and has free RAM, so it stays out of the way of whoever is at the keyboard.

    python scripts/scan_silence.py --out ../data/silence_scan [--limit N] [--no-gate]
    python scripts/scan_silence.py --out ../data/silence_scan --report   # ranked CSV
"""
from __future__ import annotations

import argparse
import csv
import ctypes
import json
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parents[1] / "audiplex.db"
FLOOR_DB = -50.0
MIN_SILENCE = 3.0   # detector floor; shorter runs are never interesting
TAIL_MIN = 3.0      # silence running to the end counts from here
GAP_MIN = 5.0       # silence in the middle, with sound after it
EDGE = 0.5          # silence starting this close to 0 is a leading silence
IDLE_NEEDED_S = 600
FREE_RAM_GB = 12
FILE_CAP_S = 180


def parse_silence(stderr: str) -> dict:
    """ffmpeg silencedetect stderr -> {total, intro, tail, gaps:[(start, dur)]}."""
    dur = re.search(r"Duration: (\d+):(\d+):([\d.]+)", stderr)
    total = int(dur[1]) * 3600 + int(dur[2]) * 60 + float(dur[3]) if dur else 0.0
    starts = [max(float(m), 0.0) for m in re.findall(r"silence_start: (-?[\d.]+)", stderr)]
    ends = [float(m) for m in re.findall(r"silence_end: ([\d.]+)", stderr)]
    intro = 0.0
    tail = None  # (start, seconds)
    gaps = []
    for i, s in enumerate(starts):
        e = ends[i] if i < len(ends) else None
        if e is None or (total and total - e < 0.1):
            e = total
            if total - s >= TAIL_MIN:
                tail = (round(s, 2), round(total - s, 2))
        elif s <= EDGE:
            intro = round(e - s, 2)
        elif e - s >= GAP_MIN:
            gaps.append((round(s, 2), round(e - s, 2)))
    return {"total": round(total, 2), "intro": intro, "tail": tail, "gaps": gaps}


def scan_file(path: str, ffmpeg: str = "ffmpeg") -> dict:
    flags = subprocess.BELOW_NORMAL_PRIORITY_CLASS if sys.platform == "win32" else 0
    out = subprocess.run(
        [ffmpeg, "-hide_banner", "-nostats", "-threads", "1", "-i", path,
         "-af", f"silencedetect=noise={FLOOR_DB}dB:d={MIN_SILENCE}", "-f", "null", "-"],
        capture_output=True, timeout=FILE_CAP_S, creationflags=flags).stderr.decode("utf-8", "replace")
    return parse_silence(out)


class _Mem(ctypes.Structure):
    _fields_ = [("l", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
        (n, ctypes.c_ulonglong) for n in ("tp", "ap", "tpf", "apf", "tv", "av", "ae")]


class _Input(ctypes.Structure):
    _fields_ = [("size", ctypes.c_uint), ("time", ctypes.c_uint)]


def gate_ok() -> tuple[bool, str]:
    """Windows only: True when input has been idle long enough and RAM is free."""
    m = _Mem()
    m.l = ctypes.sizeof(m)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
    free_gb = m.ap / 2**30
    i = _Input()
    i.size = ctypes.sizeof(i)
    ctypes.windll.user32.GetLastInputInfo(ctypes.byref(i))
    idle_s = (ctypes.windll.kernel32.GetTickCount() - i.time) / 1000
    ok = free_gb >= FREE_RAM_GB and idle_s >= IDLE_NEEDED_S
    return ok, f"free {free_gb:.1f} GB, idle {idle_s:.0f}s"


def report(out_dir: Path) -> None:
    rows = []
    for line in (out_dir / "results.jsonl").read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        if r.get("error"):
            continue
        tail = r["tail"][1] if r["tail"] else 0.0
        gap = sum(g[1] for g in r["gaps"])
        if tail or gap:
            rows.append({**r, "tail_s": tail, "gap_s": round(gap, 2), "dead_s": round(tail + gap, 2)})
    rows.sort(key=lambda r: -r["dead_s"])
    with open(out_dir / "report.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id", "title", "total_s", "tail_from_s", "tail_s", "gaps(start:secs)", "dead_s", "kind"])
        for r in rows:
            kind = "silence-then-sound" if r["gaps"] else "tail-only"
            w.writerow([r["id"], r["title"], r["total"], r["tail"][0] if r["tail"] else "", r["tail_s"],
                        ";".join(f"{s}:{d}" for s, d in r["gaps"]), r["dead_s"], kind])
    print(f"{len(rows)} tracks flagged -> {out_dir / 'report.csv'}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int, help="stop after this many new tracks")
    ap.add_argument("--no-gate", action="store_true", help="skip the idle/RAM wait (testing only)")
    ap.add_argument("--report", action="store_true", help="build report.csv from results and exit")
    a = ap.parse_args()
    out_dir = Path(a.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    if a.report:
        report(out_dir)
        return 0

    results = out_dir / "results.jsonl"
    done = set()
    if results.exists():
        done = {json.loads(l)["id"] for l in results.read_text(encoding="utf-8").splitlines() if l}
    con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    rows = con.execute("select id,title,file_path from tracks where duplicate_of is null order by id").fetchall()
    todo = [r for r in rows if r[0] not in done]
    print(f"{len(rows)} tracks, {len(done)} done, {len(todo)} to scan", flush=True)

    n = 0
    t0 = time.time()
    with open(results, "a", encoding="utf-8") as f:
        for tid, title, path in todo:
            if a.limit and n >= a.limit:
                break
            while not a.no_gate:
                ok, why = gate_ok()
                if ok:
                    break
                print(f"waiting ({why})", flush=True)
                time.sleep(30)
            try:
                r = scan_file(path)
            except (OSError, subprocess.SubprocessError) as e:
                r = {"error": str(e)[:200]}
            f.write(json.dumps({"id": tid, "title": title, **r}) + "\n")
            f.flush()
            n += 1
            if n % 200 == 0:
                print(f"{n} scanned, {n / (time.time() - t0):.2f}/s", flush=True)
    print(f"scanned {n} this run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
