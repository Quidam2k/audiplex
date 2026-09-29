"""Unwrap RIFF-wrapped MP3s back into plain MP3 files (Pantheon #5790).

Seven library files named .mp3 are ID3 tag + RIFF/WAVE container whose fmt tag is
0x55 (MPEG Layer 3): real MP3 frames inside a WAV wrapper (old RealJukebox rips).
ffprobe reads them as WAV and fails, so their duration can't be checked. The fix is
LOSSLESS: new file = original ID3 tag + the RIFF 'data' chunk bytes (the MP3 frames).
Same path, so nothing downstream changes. Report-only unless --apply.

  python scripts/unwrap_riff_mp3.py --ids 656,2011,2374,3290,3573,3723,4045
  python scripts/unwrap_riff_mp3.py --ids ... --apply
  python scripts/unwrap_riff_mp3.py --restore server/data/riff-mp3-backup-<ts>   # undo

--apply, in order: back up originals + DB, skip whatever /api/playback/state says is
playing (and everything if that can't be read), write a temp file, ffprobe it (mp3,
duration within 3 s of the DB), os.replace (a file held open = skipped), then update
ONLY file_size / file_hash / duration_seconds of that row so a rescan sees no diff.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import struct
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Callable, Optional

SERVER = Path(__file__).resolve().parents[1]
MPEG_LAYER3 = 0x55
DURATION_SLACK_S = 3.0


def split_riff_mp3(data: bytes) -> Optional[tuple[bytes, bytes]]:
    """(id3_tag, mp3_frames) when `data` is [ID3v2] + RIFF/WAVE with fmt tag 0x55, else None."""
    id3 = b""
    off = 0
    if data[:3] == b"ID3" and len(data) >= 10:
        s = data[6:10]
        size = (s[0] << 21) | (s[1] << 14) | (s[2] << 7) | s[3]
        off = 10 + size + (10 if data[5] & 0x10 else 0)
        id3 = data[:off]
    if data[off:off + 4] != b"RIFF" or data[off + 8:off + 12] != b"WAVE":
        return None
    pos, fmt_tag, frames = off + 12, None, None
    while pos + 8 <= len(data):
        cid, clen = data[pos:pos + 4], struct.unpack("<I", data[pos + 4:pos + 8])[0]
        body = data[pos + 8:pos + 8 + clen]
        if cid == b"fmt " and len(body) >= 2:
            fmt_tag = struct.unpack("<H", body[:2])[0]
        elif cid == b"data":
            frames = body
            break
        pos += 8 + clen + (clen & 1)
    if fmt_tag != MPEG_LAYER3 or not frames:
        return None
    return id3, frames


def probe(ffprobe: str, path: str) -> tuple[Optional[str], Optional[float]]:
    """(format_name, seconds) or (None, None). Never raises."""
    try:
        out = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=format_name,duration",
                              "-of", "json", path], capture_output=True, text=True, timeout=30)
        fmt = json.loads(out.stdout or "{}").get("format") or {}
        return fmt.get("format_name"), float(fmt["duration"])
    except Exception:
        return None, None


def scanner_hash(path: str) -> str:
    """Same as audiplex.utils.metadata.compute_file_hash (size:mtime md5)."""
    st = os.stat(path)
    return hashlib.md5(f"{st.st_size}:{st.st_mtime}".encode()).hexdigest()


def now_playing_paths(token_file: Path, base: str = "http://127.0.0.1:8100") -> Optional[set]:
    """File paths the server says are playing/queued-now; None when the state can't be read."""
    try:
        token = token_file.read_text(encoding="utf-8").strip()
        req = urllib.request.Request(f"{base}/api/playback/state",
                                     headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            st = json.loads(r.read())
    except Exception:
        return None
    track = st.get("track") or {}
    paths = {track.get("file_path")} if st.get("playing") else set()
    return {os.path.normcase(p) for p in paths if p}


def unwrap_one(row: tuple, ffprobe: str, apply: bool, backup_dir: Path,
               playing: set, con: Optional[sqlite3.Connection]) -> dict:
    tid, path, db_s = row
    out = {"id": tid, "path": path}
    if os.path.normcase(path) in playing:
        return {**out, "result": "skipped: playing"}
    try:
        parts = split_riff_mp3(Path(path).read_bytes())
    except OSError as e:
        return {**out, "result": f"skipped: {type(e).__name__}"}
    if parts is None:
        return {**out, "result": "skipped: not RIFF-wrapped MP3"}
    tmp = f"{path}.unwrap-tmp"
    Path(tmp).write_bytes(parts[0] + parts[1])
    fmt, secs = probe(ffprobe, tmp)
    frames_ok = (hashlib.sha256(Path(tmp).read_bytes()[len(parts[0]):]).hexdigest()
                 == hashlib.sha256(parts[1]).hexdigest())  # the MP3 frames are byte-identical
    ok = frames_ok and fmt is not None and "mp3" in fmt and secs is not None and (
        not db_s or abs(secs - db_s) <= DURATION_SLACK_S)
    out.update(format=fmt, seconds=secs, db_seconds=db_s, frames_identical=frames_ok,
               size_before=os.path.getsize(path), size_after=os.path.getsize(tmp))
    if not ok or not apply:
        os.remove(tmp)
        return {**out, "result": "would fix" if ok else "skipped: probe failed"}
    dest = backup_dir / f"{tid}_{Path(path).name}"
    shutil.copy2(path, dest)
    try:
        os.replace(tmp, path)
    except PermissionError:
        os.remove(tmp)
        return {**out, "result": "skipped: file in use"}
    con.execute("UPDATE tracks SET file_size=?, file_hash=?, duration_seconds=? WHERE id=?",
                (os.path.getsize(path), scanner_hash(path), secs, tid))
    con.commit()
    return {**out, "result": "fixed", "backup": str(dest)}


def restore(backup_dir: Path, db: str) -> int:
    """Put every original back from a backup dir written by --apply (then the DB rows)."""
    manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
    for r in manifest:
        shutil.copy2(r["backup"], r["path"])
        print("restored", r["path"])
    with sqlite3.connect(str(backup_dir / "audiplex.db.bak")) as bak, sqlite3.connect(db) as live:
        for r in manifest:
            row = bak.execute("SELECT file_size, file_hash, duration_seconds FROM tracks WHERE id=?",
                              (r["id"],)).fetchone()
            live.execute("UPDATE tracks SET file_size=?, file_hash=?, duration_seconds=? WHERE id=?",
                         (*row, r["id"]))
    return 0


def main(argv: Optional[list[str]] = None,
         playing_fn: Callable[[], Optional[set]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--ids", default="")
    ap.add_argument("--restore", default="", help="backup dir from an --apply run")
    ap.add_argument("--db", default=str(SERVER / "audiplex.db"))
    ap.add_argument("--ffprobe", default=shutil.which("ffprobe") or "ffprobe")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--backup-root", default=str(SERVER / "data"))
    a = ap.parse_args(argv)
    if a.restore:
        return restore(Path(a.restore), a.db)
    ids = [int(x) for x in a.ids.split(",") if x.strip()]
    con = sqlite3.connect(a.db)
    rows = [con.execute("SELECT id, file_path, duration_seconds FROM tracks WHERE id=?", (i,)).fetchone()
            for i in ids]
    rows = [r for r in rows if r]
    playing = (playing_fn or (lambda: now_playing_paths(SERVER.parent / ".dj_token")))()
    if playing is None:
        if a.apply:
            print("refusing --apply: can't read /api/playback/state", file=sys.stderr)
            return 2
        playing = set()
    backup_dir = Path(a.backup_root) / f"riff-mp3-backup-{int(time.time())}"
    if a.apply:
        backup_dir.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(backup_dir / "audiplex.db.bak")) as bak:
            con.backup(bak)
    results = [unwrap_one(r, a.ffprobe, a.apply, backup_dir, playing, con) for r in rows]
    if a.apply:
        (backup_dir / "manifest.json").write_text(json.dumps(
            [r for r in results if r["result"] == "fixed"], indent=1), encoding="utf-8")
    print(json.dumps({"applied": a.apply, "backup_dir": str(backup_dir) if a.apply else None,
                      "results": results}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
