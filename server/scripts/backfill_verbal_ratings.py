"""Apply the star ratings Todd said out loud before dj_star existed (#3576).

Two sources, both from the ride-era workaround:
  * Q:/Pantheon/data/music/pending_star_ratings.md — one table row per rating,
    with his words. Applied rows get an "Applied" stamp; a re-run skips them.
  * DJ tags five-star-verbal / four-half-star-verbal — any tagged track the
    table doesn't already cover is rated from the tag alone.

Writes the OWNER's track_ratings rows directly (same row the app's star tap
and PUT /api/playback/ratings write), so it does not need the new server code
deployed. A JSON copy of track_ratings is saved next to the DB first; the md
is stamped only after the DB commit succeeds, so a failed run re-runs clean.

    python scripts/backfill_verbal_ratings.py            # dry run
    python scripts/backfill_verbal_ratings.py --apply
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))

from sqlalchemy.orm import Session  # noqa: E402

from audiplex.database import get_engine  # noqa: E402
from audiplex.models import DjTrackTag, Track, TrackRating, User  # noqa: E402
from audiplex.routers.playback import stored_stars, verbal_note  # noqa: E402

PENDING_MD = Path("Q:/Pantheon/data/music/pending_star_ratings.md")
TAG_STARS = {"five-star-verbal": 5.0, "four-half-star-verbal": 4.5}
PERSONA = "ride log"


def stars_from_words(words: str) -> float | None:
    """'four, four and a half stars' -> 4.5; 'Five stars' / '5 stars' -> 5."""
    w = words.lower()
    if re.search(r"four(,| and)? (and )?a half|4\.5|four-half", w):
        return 4.5
    m = re.search(r"\b(one|two|three|four|five|[1-5])\s+stars?\b", w)
    if not m:
        return None
    return float({"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}.get(m.group(1), m.group(1)))


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def parse_table(text: str) -> tuple[list[str], int, list[dict]]:
    """(lines, header index, rows). Each row: {line_no, ids, words, applied}."""
    lines = text.splitlines()
    head = next(i for i, ln in enumerate(lines) if ln.startswith("|") and "Audiplex id" in ln)
    cols = _cells(lines[head])
    ids_col = next(i for i, c in enumerate(cols) if "Audiplex id" in c)
    words_col = next(i for i, c in enumerate(cols) if "words" in c.lower())
    applied_col = cols.index("Applied") if "Applied" in cols else None
    rows = []
    for n in range(head + 2, len(lines)):
        if not lines[n].startswith("|"):
            break
        cells = _cells(lines[n])
        # "4932 (tagged 16:40)" -> [4932]; ids only, not the times in parentheses.
        id_text = re.sub(r"\([^)]*\)", "", cells[ids_col])
        rows.append({
            "line_no": n,
            "ids": [int(x) for x in re.findall(r"\d+", id_text)],
            "words": cells[words_col].replace('"', ""),
            "applied": bool(applied_col is not None and len(cells) > applied_col and cells[applied_col]),
        })
    return lines, head, rows


def stamp_table(lines: list[str], head: int, stamps: dict[int, str]) -> str:
    """Add an Applied column (once) and fill it for the stamped line numbers."""
    out = list(lines)
    has_col = "Applied" in _cells(out[head])
    if not has_col:
        out[head] = out[head].rstrip() + " Applied |"
        out[head + 1] = out[head + 1].rstrip() + "---|"
    n = head + 2
    while n < len(out) and out[n].startswith("|"):
        if not has_col:
            out[n] = out[n].rstrip() + "  |"
        if n in stamps:
            cells = _cells(out[n])
            cells[-1] = stamps[n]
            out[n] = "| " + " | ".join(cells) + " |"
        n += 1
    return "\n".join(out) + "\n"


def plan(db: Session, rows: list[dict]) -> tuple[list[dict], list[str]]:
    """[{ids, stars, words, line_no?}] to apply, plus warnings."""
    todo, warnings, covered = [], [], set()
    for r in rows:
        covered.update(r["ids"])
        if r["applied"]:
            continue
        stars = stars_from_words(r["words"])
        if stars is None:
            warnings.append(f"line {r['line_no'] + 1}: no star count in {r['words']!r}; skipped")
            continue
        todo.append({"ids": r["ids"], "stars": stars, "words": r["words"], "line_no": r["line_no"]})
    for tag, stars in TAG_STARS.items():
        extra = sorted(t for (t,) in db.query(DjTrackTag.track_id).filter(DjTrackTag.tag == tag)
                       if t not in covered)
        if extra:
            todo.append({"ids": extra, "stars": stars, "words": f"(tagged {tag})", "line_no": None})
    return todo, warnings


def apply(db: Session, owner: User, todo: list[dict]) -> list[str]:
    """One short transaction for every rating. Returns a report line per item."""
    known = {t for (t,) in db.query(Track.id).filter(Track.id.in_([i for x in todo for i in x["ids"]]))}
    report, now = [], datetime.now(timezone.utc)
    for item in todo:
        stored, note = stored_stars(item["stars"]), verbal_note(item["stars"], item["words"], PERSONA)
        for tid in item["ids"]:
            if tid not in known:
                report.append(f"  track {tid}: NOT IN LIBRARY, skipped")
                continue
            row = db.query(TrackRating).filter_by(user_id=owner.id, track_id=tid).first()
            if row:
                row.rating, row.note, row.updated_at = stored, note, now
            else:
                db.add(TrackRating(user_id=owner.id, track_id=tid, rating=stored, note=note))
            report.append(f"  track {tid}: {item['stars']:g} -> app shows {stored}  ({note})")
    db.commit()
    return report


def fix_halves(db: Session, owner: User, apply: bool) -> list[str]:
    """#6117: ratings stored as the floor while the app had no halves get the
    exact number back from their note ("[persona, said 4.5] ...")."""
    out = []
    for row in db.query(TrackRating).filter(TrackRating.user_id == owner.id):
        m = re.search(r"said (\d(?:\.5)?)\]", row.note or "")
        if not m:
            continue
        said = float(m.group(1))
        if said != row.rating and int(said) == int(row.rating):
            out.append(f"  track {row.track_id}: {row.rating:g} -> {said:g}")
            row.rating = said
    if apply:
        db.commit()
    else:
        db.rollback()
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    ap.add_argument("--md", type=Path, default=PENDING_MD)
    ap.add_argument("--db", type=Path, default=SERVER / "audiplex.db")
    ap.add_argument("--owner", default=None, help="default: settings.dj_owner_username")
    ap.add_argument("--fix-halves", action="store_true",
                    help="#6117: restore halves floored before the app had them")
    args = ap.parse_args()

    from audiplex.config import get_settings

    owner_name = args.owner or get_settings().dj_owner_username
    lines, head, rows = parse_table(args.md.read_text(encoding="utf-8"))
    engine = get_engine(f"sqlite:///{args.db.resolve().as_posix()}")
    with Session(engine) as db:
        owner = db.query(User).filter(User.username == owner_name).first()
        if owner is None:
            print(f"Owner {owner_name!r} not found.")
            return 1
        if args.fix_halves:
            fixed = fix_halves(db, owner, args.apply)
            print("\n".join(fixed) or "No floored halves.")
            print("Applied." if args.apply and fixed else "Dry run. --apply to write." if fixed else "")
            return 0
        todo, warnings = plan(db, rows)
        for w in warnings:
            print("WARN", w)
        if not todo:
            print("Nothing to apply.")
            return 0
        for item in todo:
            print(f"{item['stars']:g} stars -> {item['ids']}  {item['words']!r}")
        if not args.apply:
            print("Dry run. --apply to write.")
            return 0
        backup = args.db.with_name(f"{args.db.name}.bak-3576-ratings-{int(time.time())}.json")
        backup.write_text(json.dumps([
            {"id": r.id, "user_id": r.user_id, "track_id": r.track_id, "rating": r.rating,
             "note": r.note, "updated_at": str(r.updated_at)} for r in db.query(TrackRating)
        ], indent=1), encoding="utf-8")
        print(f"Backed up track_ratings -> {backup}")
        print("\n".join(apply(db, owner, todo)))

    today = datetime.now().strftime("%Y-%m-%d")
    stamps = {i["line_no"]: f"{today}: {i['stars']:g} (app {stored_stars(i['stars'])})"
              for i in todo if i["line_no"] is not None}
    if stamps:
        args.md.write_text(stamp_table(lines, head, stamps), encoding="utf-8")
        print(f"Stamped {len(stamps)} row(s) applied in {args.md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
