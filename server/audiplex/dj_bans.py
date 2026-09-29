"""DJ bans (#2806): tracks the DJ must never pick again, by recording identity."""

from sqlalchemy.orm import Session

from audiplex.models import DjBan


def banned_ids(db: Session, identities: dict | None = None) -> set[int]:
    """Every banned track id, plus every other copy of the same recording.

    identities: identity.build_identity_map(db) if the caller already has it.
    """
    ids = {r[0] for r in db.query(DjBan.track_id).all()}
    if not ids:
        return set()
    if identities is None:
        from audiplex.identity import build_identity_map

        identities = build_identity_map(db)
    recordings = {identities[i].recording_id for i in ids if i in identities}
    if recordings:
        ids |= {tid for tid, ident in identities.items() if ident.recording_id in recordings}
    return ids
