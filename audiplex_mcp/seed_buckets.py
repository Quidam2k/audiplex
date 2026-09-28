"""Seed the planned themed buckets from the live library (#5518).

    python -m audiplex_mcp.seed_buckets            # write
    python -m audiplex_mcp.seed_buckets --dry-run  # show what would land

Hand-picked from track titles on 2026-09-28 (Todd's examples: bravery, protest,
sunset on the river; plus 'on the road' for rides). One copy per song; spoken
word and movie quotes left out. Idempotent: each run replaces the planned
bucket's tracks with this list. Track ids are checked against the catalog DB
(read-only) and their file paths stored so a rescan can be repaired later.
"""

import argparse
import os
import sqlite3
import sys
from pathlib import Path

from audiplex_mcp import buckets

SEEDS = {
    "bravery": (
        "Courage, standing tall, getting back up.",
        [14, 123, 2616, 2601, 5395, 1808, 1785, 3825, 712],
    ),
    "protest": (
        "Protest, peace and freedom songs.",
        [1759, 1059, 1066, 4449, 1055, 2496, 2862, 4649, 1825, 61, 1141],
    ),
    "sunset on the river": (
        "Easy evening and water songs for the golden hour.",
        [56, 1649, 1742, 914, 916, 917, 920, 921, 922, 2427, 3473, 4613, 4553, 4170, 926],
    ),
    "on the road": (
        "Road and journey songs for the ride.",
        [1765, 3845, 603, 605, 572, 2429, 3490, 3871, 2247, 595, 5516, 2530, 2480],
    ),
}


def _catalog_db() -> Path:
    return Path(os.environ.get("AUDIPLEX_DB") or Path(__file__).resolve().parent.parent / "server" / "audiplex.db")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    conn = sqlite3.connect(f"file:{_catalog_db()}?mode=ro", uri=True)
    try:
        for name, (desc, ids) in SEEDS.items():
            found = dict(
                conn.execute(
                    f"SELECT id, file_path FROM tracks WHERE id IN ({','.join('?' * len(ids))})", ids
                ).fetchall()
            )
            missing = [i for i in ids if i not in found or not Path(found[i]).exists()]
            tracks = [{"track_id": i, "path": found[i]} for i in ids if i not in missing]
            print(f"{name}: {len(tracks)} tracks" + (f", MISSING {missing}" if missing else ""))
            if not args.dry_run:
                buckets.save_bucket(name, desc, "planned", "orolo", tracks, replace=True)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
