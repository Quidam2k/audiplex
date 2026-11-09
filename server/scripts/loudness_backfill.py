"""#5683: idle-gated one-off backfill of loudness (LUFS) and true peak (dBTP) into the Audiplex DB."""
import argparse
import ctypes
import os
import sqlite3
import sys
import time
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

import psutil

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from audiplex.loudness_live import measure_loudness  # noqa: E402
from lufs_when_idle import streaming  # noqa: E402

TABLES = {"book": "books", "track": "tracks"}


class LII(ctypes.Structure):  # #5683
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def input_idle_s():  # #5683
    li = LII()
    li.cbSize = ctypes.sizeof(LII)
    ok = ctypes.windll.user32.GetLastInputInfo(ctypes.byref(li))
    if not ok:
        return 0.0
    return ((ctypes.windll.kernel32.GetTickCount() - li.dwTime) & 0xFFFFFFFF) / 1000


def gate_ok(ram_gb, idle_s, phone_busy, min_ram_gb=12.0, min_idle_s=600):  # #5683
    return ram_gb >= min_ram_gb and idle_s >= min_idle_s and not phone_busy


def select_groups(con, target_lufs, max_tracks):  # #5683
    def books(where):  # #5683
        q = f"SELECT id, file_path FROM books WHERE {where} ORDER BY id"
        return [("book", i, p) for i, p in con.execute(q)]

    q4 = (
        "SELECT t.id, t.file_path FROM tracks t "
        "LEFT JOIN (SELECT track_id, COUNT(*) n FROM play_stats GROUP BY track_id) p "
        "ON p.track_id = t.id "
        "WHERE t.content_kind = 'music' AND t.true_peak_dbtp IS NULL AND t.loudness_lufs < ? "
        "ORDER BY COALESCE(n, 0) DESC, t.loudness_lufs ASC, t.id ASC"
    )
    args = [target_lufs - 6.0]
    if max_tracks > 0:
        q4 += " LIMIT ?"
        args.append(max_tracks)
    q3 = ("SELECT id, file_path FROM tracks "
          "WHERE content_kind = 'music' AND loudness_lufs IS NULL ORDER BY id")
    return [
        ("meditation books", books("category = 'meditation' AND "
                                   "(loudness_lufs IS NULL OR true_peak_dbtp IS NULL)")),
        ("other books", books("category IS NOT 'meditation' AND loudness_lufs IS NULL")),
        ("music tracks without lufs", [("track", i, p) for i, p in con.execute(q3)]),
        ("quiet tracks without peak", [("track", i, p) for i, p in con.execute(q4, args)]),
    ]


def select_todo(con, target_lufs, max_tracks):  # #5683
    return [row for _, rows in select_groups(con, target_lufs, max_tracks) for row in rows]


def store(con, kind, id, lufs, peak):  # #5683
    table = TABLES[kind]
    con.execute(
        f"UPDATE {table} SET "
        "loudness_lufs = CASE WHEN loudness_lufs IS NULL AND ? IS NOT NULL THEN ? "
        "ELSE loudness_lufs END, "
        "true_peak_dbtp = COALESCE(?, true_peak_dbtp) WHERE id = ?",
        (lufs, lufs, peak, id),
    )
    con.commit()


def run(con, todo, *, measure=measure_loudness, gate=lambda: True,
        sleep=time.sleep, emit=print, poll_s=60.0):  # #5683
    res = {"measured": 0, "failed": 0, "missing": 0}
    total = len(todo)
    for n, (kind, id, path) in enumerate(todo, 1):
        if not path or not os.path.exists(path):
            res["missing"] += 1
        else:
            while not gate():
                sleep(poll_s)
            try:
                lufs, peak = measure(path)
            except Exception:
                lufs = peak = None
            if lufs is None and peak is None:
                res["failed"] += 1
            else:
                store(con, kind, id, lufs, peak)
                res["measured"] += 1
        if n % 25 == 0:
            emit(f"progress {n}/{total} failed={res['failed']}")
    return res


def make_gate(min_ram_gb, min_idle_s, emit=print):  # #5683
    state = {"waiting": False}

    def gate():  # #5683
        ram = psutil.virtual_memory().available / 2**30
        idle = input_idle_s()
        phone = bool(streaming(time.time()))
        ok = gate_ok(ram, idle, phone, min_ram_gb, min_idle_s)
        if not ok and not state["waiting"]:
            emit(f"wait ram={ram:.1f} idle={idle:.0f} phone={phone}")
        state["waiting"] = not ok
        return ok

    return gate


def backup_db(con, db):  # #5683
    dst_path = f"{db}.bak-5683-{datetime.now().strftime('%Y%m%dT%H%M%S')}"
    dst = sqlite3.connect(dst_path)
    try:
        con.backup(dst)
    finally:
        dst.close()
    return dst_path


def main(argv=None):  # #5683
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(SERVER / "audiplex.db"))
    ap.add_argument("--target-lufs", type=float, default=-14.0)
    ap.add_argument("--max-tracks", type=int, default=0)
    ap.add_argument("--poll-s", type=float, default=60)
    ap.add_argument("--min-ram-gb", type=float, default=12.0)
    ap.add_argument("--min-idle-s", type=float, default=600)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    con = sqlite3.connect(a.db)
    try:
        groups = select_groups(con, a.target_lufs, a.max_tracks)
        if a.dry_run:
            for name, rows in groups:
                print(f"{name}: {len(rows)}")
            return 0
        todo = [row for _, rows in groups for row in rows]
        if todo:
            print(f"backup: {backup_db(con, a.db)}")
        res = run(con, todo, gate=make_gate(a.min_ram_gb, a.min_idle_s),
                  poll_s=a.poll_s)
    finally:
        con.close()
    print(f"done: measured={res['measured']} failed={res['failed']} missing={res['missing']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
