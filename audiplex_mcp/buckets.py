"""Themed music buckets: curated sets of tracks for a theme, mood or occasion (#5518).

A bucket is a named, ordered list of track ids ("bravery", "sunset on the
river"). It becomes a DJ lane through the shared source resolver
(kind='bucket'), so it rides the pool's lane machinery rather than a second
system. origin='planned' buckets are seeded ahead of time; origin='dj' ones
are built by the DJ personas themselves and refined ride over ride.

The path is kept next to each track id so a library rescan that renumbers
ids can be repaired later instead of silently losing the bucket.
"""

import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

ORIGINS = ("planned", "dj")


def _db_path() -> Path:
    """Read at call time so tests can point it elsewhere."""
    return Path(
        os.environ.get("DJ_BUCKETS_DB")
        or Path(__file__).resolve().parent.parent / "data" / "dj" / "buckets.db"
    )


@contextmanager
def _db():
    """Open (creating on first use), commit, and always close — long-lived process."""
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS buckets (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL UNIQUE COLLATE NOCASE,
            description TEXT NOT NULL DEFAULT '',
            origin      TEXT NOT NULL DEFAULT 'planned' CHECK(origin IN ('planned','dj')),
            created_by  TEXT NOT NULL DEFAULT '',
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS bucket_tracks (
            bucket_id INTEGER NOT NULL REFERENCES buckets(id) ON DELETE CASCADE,
            track_id  INTEGER NOT NULL,
            path      TEXT NOT NULL DEFAULT '',
            added_by  TEXT NOT NULL DEFAULT '',
            note      TEXT NOT NULL DEFAULT '',
            added_at  TEXT NOT NULL,
            position  INTEGER NOT NULL,
            PRIMARY KEY (bucket_id, track_id)
        )"""
        )
        yield conn
        conn.commit()
    finally:
        conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_name(name) -> str:
    n = re.sub(r"\s+", " ", str(name or "")).strip().lower()
    if not n:
        raise ValueError("A bucket needs a name.")
    return n


def _norm_tracks(tracks) -> list[dict]:
    out = []
    for t in tracks or []:
        if isinstance(t, dict):
            tid, path, note = t.get("track_id", t.get("id")), t.get("path", ""), t.get("note", "")
        else:
            tid, path, note = t, "", ""
        try:
            ok = not isinstance(tid, bool) and int(tid) > 0 and float(tid) == int(tid)
        except (TypeError, ValueError):
            ok = False
        if not ok:
            raise ValueError(f"Bad track id: {tid!r}.")
        tid = int(tid)
        out.append({"track_id": tid, "path": str(path or ""), "note": str(note or "")})
    return out


def _row(conn, name: str):
    return conn.execute("SELECT * FROM buckets WHERE name = ?", (normalize_name(name),)).fetchone()


def _count(conn, bucket_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM bucket_tracks WHERE bucket_id = ?", (bucket_id,)
    ).fetchone()[0]


def _append(conn, bucket_id: int, tracks: list[dict], added_by: str) -> tuple[int, int]:
    """Append tracks after the current last position; duplicates are skipped."""
    pos = conn.execute(
        "SELECT COALESCE(MAX(position), -1) FROM bucket_tracks WHERE bucket_id = ?", (bucket_id,)
    ).fetchone()[0]
    added = skipped = 0
    now = _now()
    for t in tracks:
        cur = conn.execute(
            "INSERT OR IGNORE INTO bucket_tracks"
            " (bucket_id, track_id, path, added_by, note, added_at, position)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (bucket_id, t["track_id"], t["path"], added_by, t["note"], now, pos + 1),
        )
        if cur.rowcount:
            added += 1
            pos += 1
        else:
            skipped += 1
    return added, skipped


def list_buckets() -> list[dict]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT b.*, (SELECT COUNT(*) FROM bucket_tracks t WHERE t.bucket_id = b.id)"
            " AS track_count FROM buckets b ORDER BY b.name"
        ).fetchall()
    return [
        {k: r[k] for k in ("name", "description", "origin", "created_by", "track_count", "updated_at")}
        for r in rows
    ]


def get_bucket(name) -> dict | None:
    with _db() as conn:
        b = _row(conn, name)
        if not b:
            return None
        tracks = conn.execute(
            "SELECT track_id, path, note, added_by FROM bucket_tracks"
            " WHERE bucket_id = ? ORDER BY position",
            (b["id"],),
        ).fetchall()
    out = {k: b[k] for k in ("name", "description", "origin", "created_by", "created_at", "updated_at")}
    out["tracks"] = [dict(t) for t in tracks]
    return out


def find_bucket(query) -> dict | None:
    """Exact name first, then a unique substring match; ambiguity is an error."""
    q = normalize_name(query)
    exact = get_bucket(q)
    if exact:
        return exact
    hits = [b["name"] for b in list_buckets() if q in b["name"]]
    if len(hits) > 1:
        raise ValueError(f"'{query}' matches several buckets: {', '.join(hits)}.")
    return get_bucket(hits[0]) if hits else None


def save_bucket(name, description="", origin="planned", created_by="", tracks=None, replace=False) -> dict:
    if origin not in ORIGINS:
        raise ValueError(f"origin must be one of {', '.join(ORIGINS)}.")
    n = normalize_name(name)
    norm = _norm_tracks(tracks)
    now = _now()
    with _db() as conn:
        b = _row(conn, n)
        created = b is None
        if created:
            bid = conn.execute(
                "INSERT INTO buckets (name, description, origin, created_by, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (n, description or "", origin, created_by or "", now, now),
            ).lastrowid
        else:
            bid = b["id"]
            if replace:
                conn.execute("DELETE FROM bucket_tracks WHERE bucket_id = ?", (bid,))
                conn.execute(
                    "UPDATE buckets SET description = ?, origin = ?, updated_at = ? WHERE id = ?",
                    (description or "", origin, now, bid),
                )
            else:
                if description:
                    conn.execute("UPDATE buckets SET description = ? WHERE id = ?", (description, bid))
                conn.execute("UPDATE buckets SET updated_at = ? WHERE id = ?", (now, bid))
        added, skipped = _append(conn, bid, norm, created_by or "")
        count = _count(conn, bid)
    return {"name": n, "created": created, "added": added, "skipped_duplicates": skipped, "track_count": count}


def add_tracks(name, tracks, added_by="") -> dict:
    norm = _norm_tracks(tracks)
    with _db() as conn:
        b = _row(conn, name)
        if not b:
            raise ValueError(f"No bucket named '{name}'.")
        added, skipped = _append(conn, b["id"], norm, added_by or "")
        conn.execute("UPDATE buckets SET updated_at = ? WHERE id = ?", (_now(), b["id"]))
        count = _count(conn, b["id"])
    return {"name": b["name"], "added": added, "skipped_duplicates": skipped, "track_count": count}


def remove_tracks(name, track_ids) -> dict:
    ids = [t["track_id"] for t in _norm_tracks(track_ids)]
    with _db() as conn:
        b = _row(conn, name)
        if not b:
            raise ValueError(f"No bucket named '{name}'.")
        removed = 0
        for tid in ids:
            removed += conn.execute(
                "DELETE FROM bucket_tracks WHERE bucket_id = ? AND track_id = ?", (b["id"], tid)
            ).rowcount
        conn.execute("UPDATE buckets SET updated_at = ? WHERE id = ?", (_now(), b["id"]))
        count = _count(conn, b["id"])
    return {"name": b["name"], "removed": removed, "track_count": count}


def delete_bucket(name) -> bool:
    with _db() as conn:
        return conn.execute("DELETE FROM buckets WHERE name = ?", (normalize_name(name),)).rowcount > 0


def track_ids(name) -> list[int]:
    b = get_bucket(name)
    if b is None:
        raise ValueError(f"No bucket named '{name}'.")
    return [t["track_id"] for t in b["tracks"]]
