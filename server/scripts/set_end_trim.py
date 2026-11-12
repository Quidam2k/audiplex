"""#5682: find trailing silence on tracks and set tracks.end_trim_seconds.

The stream route then serves a copy cut there; the music file is never edited.
Dry run unless --apply. Only silence that runs to the very end of the file is
trimmed, and a tail with sound above the floor is reported, not cut.

    python scripts/set_end_trim.py --like "Belleville Rendez" [--apply]
    python scripts/set_end_trim.py --ids 344,1943 --apply
    python scripts/set_end_trim.py --ids 344 --clear --apply
    python scripts/set_end_trim.py --ids 1716 --seconds 159.9 --apply   # #5893: a chosen cut point
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parents[1] / "audiplex.db"
FLOOR_DB = -50.0   # at or under this is silence
MIN_SILENCE = 1.0  # a tail shorter than this is left alone
PAD = 0.5          # keep this much after the last sound so the note rings out


def tail_silence(path: str, ffmpeg: str = "ffmpeg") -> tuple[float, float | None]:
    """(file seconds, start of a silence that runs to the end, or None)."""
    out = subprocess.run(
        [ffmpeg, "-hide_banner", "-nostats", "-i", path,
         "-af", f"silencedetect=noise={FLOOR_DB}dB:d={MIN_SILENCE}", "-f", "null", "-"],
        capture_output=True, text=True, timeout=300).stderr
    dur = re.search(r"Duration: (\d+):(\d+):([\d.]+)", out)
    total = int(dur[1]) * 3600 + int(dur[2]) * 60 + float(dur[3]) if dur else 0.0
    starts = [float(m) for m in re.findall(r"silence_start: ([\d.]+)", out)]
    ends = [float(m) for m in re.findall(r"silence_end: ([\d.]+)", out)]
    if starts and len(starts) > len(ends):
        return total, starts[-1]  # silence never ended: runs to the end
    if starts and ends and total - ends[-1] < 0.1:
        return total, starts[-1]  # ended at the final frame
    return total, None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--ids", help="comma-separated track ids")
    ap.add_argument("--like", help="title substring")
    ap.add_argument("--clear", action="store_true", help="remove the trim instead")
    ap.add_argument("--seconds", type=float, help="#5893: cut here instead of at detected silence (e.g. a movie scene after the song)")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    if not a.ids and not a.like:
        ap.error("give --ids or --like")
    if a.seconds is not None and a.clear:
        ap.error("--seconds and --clear do not mix")

    con = sqlite3.connect(a.db)
    cols = {r[1] for r in con.execute("pragma table_info(tracks)")}
    if "end_trim_seconds" not in cols:
        print("tracks.end_trim_seconds is missing: restart the server once so it migrates", file=sys.stderr)
        return 2
    if a.ids:
        ids = [int(x) for x in a.ids.split(",")]
        rows = con.execute(f"select id,title,file_path,end_trim_seconds from tracks where id in ({','.join('?' * len(ids))})", ids).fetchall()
    else:
        rows = con.execute("select id,title,file_path,end_trim_seconds from tracks where title like ?", (f"%{a.like}%",)).fetchall()

    for tid, title, path, current in rows:
        if a.clear:
            new = None
            note = "clear"
        else:
            try:
                total, start = tail_silence(path)
            except (OSError, subprocess.SubprocessError) as e:
                print(f"{tid} {title!r}: skipped ({e})")
                continue
            if a.seconds is not None:
                if not 0 < a.seconds < total:
                    print(f"{tid} {title!r}: --seconds {a.seconds} is outside the file (0-{total:.2f}s), left alone")
                    continue
                new = round(a.seconds, 3)
                note = f"file {total:.2f}s, cut at {new}s by hand (drops {total - new:.2f}s)"
            elif start is None:
                print(f"{tid} {title!r}: no silence at the end ({total:.1f}s), left alone")
                continue
            else:
                new = round(min(start + PAD, total), 3)
                note = f"file {total:.2f}s, silence from {start:.2f}s, cut at {new}s (drops {total - new:.2f}s)"
        print(f"{tid} {title!r}: {note}" + (" [applied]" if a.apply else " [dry run]"))
        if a.apply and new != current:
            con.execute("update tracks set end_trim_seconds=? where id=?", (new, tid))
    con.commit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
