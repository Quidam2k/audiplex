"""Hide duplicate copies of one recording from flat track lists (#8707).

Todd keeps copies of the same song on purpose in mix folders (e.g. "Bike
Music", "Individual\faster\On Playa") next to the real album under
"...\Artists & Albums\<genre>\...". Files are never touched. Flat lists
(artist tracks, genre tracks, search) should show ONE entry per recording;
the mix folder's own album/folder view, playlists and the DJ keep every copy
so a mix still plays whole.

Track.duplicate_of holds the canonical track id; NULL = canonical or unique.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from audiplex.identity import build_identity_map, group_ids_by_recording
from audiplex.models import Track

def canonical_rank(file_path: str, track_id: int) -> tuple:
    """Sort key, lower = better canonical. Real album copy beats mix copies."""
    path = (file_path or "").lower().replace("\\", "/")
    real_album = "/artists & albums/" in path and "/mixed/" not in path
    return (0 if real_album else 1, track_id)


def mark_duplicates(db: Session) -> dict:
    """Recompute Track.duplicate_of for the whole library. Caller commits."""
    groups = group_ids_by_recording(build_identity_map(db))
    current = {
        tid: (path, dup)
        for tid, path, dup in db.query(Track.id, Track.file_path, Track.duplicate_of).all()
    }

    # Desired value per track: canonical id for hidden copies, None otherwise.
    wanted: dict[int, int | None] = {}
    multi = 0
    for ids in groups.values():
        ids = [i for i in ids if i in current]
        if len(ids) < 2:
            continue
        multi += 1
        canonical = min(ids, key=lambda i: canonical_rank(current[i][0], i))
        for tid in ids:
            wanted[tid] = None if tid == canonical else canonical

    # Only write rows whose value changes, grouped by target value.
    by_target: dict[int | None, list[int]] = {}
    for tid, (_, old) in current.items():
        new = wanted.get(tid)
        if new != old:
            by_target.setdefault(new, []).append(tid)

    changed = 0
    for target, ids in by_target.items():
        db.query(Track).filter(Track.id.in_(ids)).update(
            {Track.duplicate_of: target}, synchronize_session=False
        )
        changed += len(ids)

    hidden = sum(1 for tid in current if wanted.get(tid) is not None)
    return {"groups": multi, "hidden": hidden, "changed": changed}
