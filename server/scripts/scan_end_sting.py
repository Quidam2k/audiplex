"""#6991 follow-up to #6710: read-only scan for tracks that END with a short burst of
sound after a silence gap (a song, ~2 s of silence, a 5 s Netflix sting, silence to EOF).
scan_silence.py missed these: its silence floor is 3 s. Report only, it never sets a
trim: a sting and a hidden track look alike, so which to cut is Todd's call.

    py -3.11 scripts/scan_end_sting.py --out ../data/silence_scan [--limit N] [--no-gate]
    py -3.11 scripts/scan_end_sting.py --out ../data/silence_scan --report   # end_sting.md
"""
import argparse
import json
import re
import subprocess
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import scan_silence as ss  # noqa: E402

QUERY = ("select id,title,file_path,duration_seconds from tracks where duplicate_of is null "
         "and content_kind='music' and duration_seconds between 20 and 1800 order by id")
FLAGS = subprocess.BELOW_NORMAL_PRIORITY_CLASS if sys.platform == "win32" else 0


def find_end_sting(starts, ends, window_start, total):
    """starts/ends are absolute times. Returns gap/sound info for the last qualifying gap, or None."""
    starts, ends = list(starts), list(ends)
    if len(ends) > len(starts):  # silence began before the window
        starts.insert(0, window_start)
    runs = [(s, ends[i] if i < len(ends) else total) for i, s in enumerate(starts)]
    found = None
    for i, (gs, ge) in enumerate(runs):
        if not (ge < total - 0.3 and gs > window_start + 0.5):
            continue
        if i + 1 < len(runs):
            ns, ne = runs[i + 1]
            if ne < total - 0.3:
                continue
            s_end = ns
        else:
            s_end = total
        length = s_end - ge
        if 0.5 <= length <= 10.0:
            found = {"gap_start": round(gs, 2), "gap_s": round(ge - gs, 2),
                     "sound_start": round(ge, 2), "sound_s": round(length, 2)}
    return found


def ffmpeg(args):
    p = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-threads", "1", *args],
                       capture_output=True, timeout=60, creationflags=FLAGS)
    return p.returncode, p.stderr.decode("utf-8", errors="replace")


def peak_db(path, start, length):
    _, err = ffmpeg(["-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", path, "-af",
                     "astats=measure_overall=Peak_level:measure_perchannel=none", "-f", "null", "-"])
    m = re.findall(r"Peak level dB: (-?[\d.]+|-inf)", err)
    if not m or m[-1] == "-inf":
        return None
    return float(m[-1])


def scan_track(tid, title, path, total):
    rc, err = ffmpeg(["-sseof", "-40", "-i", path, "-af", "silencedetect=noise=-45dB:d=1.0",
                      "-f", "null", "-"])
    if rc != 0:
        raise RuntimeError(f"ffmpeg rc={rc}: {err.strip()[-150:]}")
    # The file's own length, not the DB's: -sseof counts back from the real end.
    dur = re.search(r"Duration: (\d+):(\d+):([\d.]+)", err)
    if dur:
        total = int(dur[1]) * 3600 + int(dur[2]) * 60 + float(dur[3])
    ws = max(0.0, total - 40)
    starts = [ws + float(x) for x in re.findall(r"silence_start: (-?[\d.]+)", err)]
    ends = [ws + float(x) for x in re.findall(r"silence_end: ([\d.]+)", err)]
    rec = {"id": tid, "title": title, "total": total, "candidate": False}
    hit = find_end_sting(starts, ends, ws, total)
    if hit:
        rec["candidate"] = True
        rec.update(hit)
        rec["sound_peak_db"] = peak_db(path, hit["sound_start"], hit["sound_s"])
        gs = hit["gap_start"]
        rec["song_peak_db"] = peak_db(path, max(0.0, gs - 10), min(10.0, gs))
    return rec


def mmss(x):
    x = int(round(x))
    return f"{x // 60}:{x % 60:02d}"


def report(out):
    rows = []
    for line in (out / "end_sting.jsonl").read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("candidate"):
            rows.append(r)
    rows.sort(key=lambda r: (r["sound_s"], r["id"]))
    fmt = lambda v: "-inf" if v is None else f"{v:.1f}"
    lines = ["| id | title | length | gap at | gap s | sound s | sound peak dB | song peak dB |",
             "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        title = str(r["title"]).replace("|", "\\|")
        lines.append(f"| {r['id']} | {title} | {mmss(r['total'])} | {mmss(r['gap_start'])} | "
                     f"{r['gap_s']} | {r['sound_s']} | {fmt(r['sound_peak_db'])} | {fmt(r['song_peak_db'])} |")
    (out / "end_sting.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{len(rows)} candidates")


def main():
    ap = argparse.ArgumentParser(description="Find tracks ending with a sting after a silence gap.")
    ap.add_argument("--db", default=ss.DEFAULT_DB)
    ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--no-gate", action="store_true")
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    jl = out / "end_sting.jsonl"
    if a.report:
        report(out)
        return
    done = set()
    if jl.exists():
        for line in jl.read_text(encoding="utf-8").splitlines():
            try:
                done.add(json.loads(line)["id"])
            except (ValueError, KeyError):
                pass
    con = sqlite3.connect(f"file:{Path(a.db).as_posix()}?mode=ro", uri=True)
    try:
        tracks = [t for t in con.execute(QUERY) if t[0] not in done]
    finally:
        con.close()
    if a.limit:
        tracks = tracks[:a.limit]
    with open(jl, "a", encoding="utf-8") as f:
        for n, (tid, title, path, total) in enumerate(tracks, 1):
            if not a.no_gate:
                ok, why = ss.gate_ok()
                while not ok:
                    print(f"waiting: {why}", flush=True)
                    time.sleep(30)
                    ok, why = ss.gate_ok()
            try:
                rec = scan_track(tid, title, path, float(total))
            except Exception as e:
                rec = {"id": tid, "title": title, "error": str(e)[:200]}
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
            if n % 200 == 0:
                print(f"{n}/{len(tracks)} scanned", flush=True)
    print(f"done: {len(tracks)} scanned", flush=True)


if __name__ == "__main__":
    main()
