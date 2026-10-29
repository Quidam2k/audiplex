"""#10072: give books the #8707 tidy re-created on 10/21 their original added_at back.

The tidy merged chapter-split books into new rows, so their added_at became
the merge time and they crowd the app's "Recently added" chip. The only
evidence trusted here is the pre-merge DB backup: a row is changed only when
its folder held books in that backup with an added_at more than a day OLDER
than the current value. Folder times are not used (a copy or move resets them).

Dry run by default. --apply backs up the DB first, writes a log of old/new
pairs next to it, and prints the one-line revert.
"""

import argparse
import json
import shutil
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent
DB = SERVER / "audiplex.db"
PRE_MERGE = SERVER / "audiplex.db.bak-nightly-energy-20261021T003008"


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace(" ", "T"))


def plan(db: sqlite3.Connection, old: sqlite3.Connection) -> list[tuple[int, str, str, str]]:
    old_rows = old.execute("SELECT file_path, added_at FROM books").fetchall()
    changes = []
    for book_id, title, folder, added in db.execute(
        "SELECT id, title, file_path, added_at FROM books "
        "WHERE category = 'audiobook_misc' AND added_at LIKE '2026-10-21%'"
    ):
        prefix = folder.rstrip("\\/") + "\\"
        older = [a for p, a in old_rows if p == folder or p.startswith(prefix)]
        if older and _ts(min(older)) < _ts(added) - timedelta(days=1):
            changes.append((book_id, title, added, min(older)))
    return changes


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    db = sqlite3.connect(DB)
    old = sqlite3.connect(f"file:{PRE_MERGE}?mode=ro", uri=True)
    changes = plan(db, old)
    for book_id, title, cur, new in changes:
        print(f"{book_id}  {cur[:19]} -> {new[:19]}  {title}")
    print(f"{len(changes)} row(s) to change")
    if not args.apply or not changes:
        return

    stamp = time.strftime("%Y%m%dT%H%M%S")
    backup = SERVER / f"audiplex.db.bak-10072-{stamp}"
    shutil.copy2(DB, backup)
    log = SERVER / f"audiplex.db.bak-10072-{stamp}.added_at.json"
    log.write_text(json.dumps(
        [{"id": i, "title": t, "old": c, "new": n} for i, t, c, n in changes], indent=1
    ), encoding="utf-8")
    with db:
        db.executemany("UPDATE books SET added_at = ? WHERE id = ?",
                       [(n, i) for i, _, _, n in changes])
    print(f"applied; backup {backup.name}; log {log.name}")
    print("revert: C:\\Python311\\python.exe -c \"import json,sqlite3; c=sqlite3.connect('audiplex.db'); "
          f"[c.execute('UPDATE books SET added_at=? WHERE id=?',(r['old'],r['id'])) for r in json.load(open('{log.name}'))]; c.commit()\"")


if __name__ == "__main__":
    main()
