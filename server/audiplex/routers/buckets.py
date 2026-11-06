"""DJ buckets over REST for the browser UI (#11055).

Thin wrapper over audiplex_mcp/buckets.py, the same store the DJ MCP tools and the
ride lane resolver read; no second store. Auth matches Playlists (any signed-in user).
"""

import ntpath
import sys
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from audiplex.auth import get_current_user
from audiplex.database import get_db
from audiplex.models import Track

router = APIRouter(prefix="/api/dj/buckets", tags=["dj_buckets"])


def _buckets():
    try:
        from audiplex_mcp import buckets
    except ImportError:  # the server runs from server/
        sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
        from audiplex_mcp import buckets
    return buckets


def _queue_for_loudness(track_ids: list[int]) -> None:
    """#11070: web adds get LUFS-measured first, exactly like the MCP's dj_bucket_save (#7387)."""
    _buckets()  # puts the repo root on sys.path when the server runs from server/
    from audiplex_mcp import bucket_tools

    bucket_tools._queue_for_loudness(track_ids)


class BucketCreate(BaseModel):
    name: str
    description: str = ""


class BucketTracks(BaseModel):
    track_ids: list[int]


class BucketDescription(BaseModel):
    description: str


@router.get("")
def list_buckets(user=Depends(get_current_user)):
    return _buckets().list_buckets()


@router.post("", status_code=201)
def create_bucket(body: BucketCreate, user=Depends(get_current_user)):
    store = _buckets()
    try:
        name = store.normalize_name(body.name)
        if store.get_bucket(name) is not None:
            raise HTTPException(409, f"A bucket named '{name}' already exists.")
        return store.save_bucket(
            name, body.description, origin="planned", created_by=user.username
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/{name}")
def get_bucket(name: str, db: Session = Depends(get_db), user=Depends(get_current_user)):
    bucket = _buckets().get_bucket(name)
    if bucket is None:
        raise HTTPException(404, "Bucket not found.")
    ids = [row["track_id"] for row in bucket["tracks"]]
    query = select(Track).where(Track.id.in_(ids)).options(
        joinedload(Track.artist), joinedload(Track.album)
    )
    library = {track.id: track for track in db.scalars(query)}
    for row in bucket["tracks"]:
        track = library.get(row["track_id"])
        row.update(
            id=row["track_id"],  # the browser's track rows key on id
            title=track.title if track else (
                ntpath.basename(row.get("path") or "") or f"Track {row['track_id']}"
            ),
            artist_name=track.artist.name if track and track.artist else None,
            album=track.album.title if track and track.album else None,
            duration_seconds=track.duration_seconds if track else 0,
            missing=track is None,
        )
    return bucket


@router.post("/{name}/tracks")
def add_tracks(name: str, body: BucketTracks, db: Session = Depends(get_db),
               user=Depends(get_current_user)):
    ids = list(dict.fromkeys(body.track_ids))
    library = {t.id: t for t in db.scalars(select(Track).where(Track.id.in_(ids)))}
    tracks = [{"track_id": i, "path": library[i].file_path} for i in ids if i in library]
    try:
        result = _buckets().add_tracks(name, tracks, added_by=user.username)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    _queue_for_loudness([t["track_id"] for t in tracks])  # #11070
    return {**result, "unknown_ids": [i for i in ids if i not in library]}


@router.delete("/{name}/tracks")
def remove_tracks(name: str, body: BucketTracks, user=Depends(get_current_user)):
    try:
        return _buckets().remove_tracks(name, body.track_ids)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.delete("/{name}")
def delete_bucket(name: str, user=Depends(get_current_user)):
    # 200 with a body, not 204: the browser's api() returns null for 204 and its
    # callers read null as failure.
    if not _buckets().delete_bucket(name):
        raise HTTPException(404, "Bucket not found.")
    return {"deleted": True}


@router.put("/{name}")
def update_description(name: str, body: BucketDescription, user=Depends(get_current_user)):
    store = _buckets()
    existing = store.get_bucket(name)
    if existing is None:
        raise HTTPException(404, "Bucket not found.")
    if body.description == "":
        raise HTTPException(400, "Description can't be blank.")
    store.save_bucket(
        existing["name"], description=body.description, origin=existing["origin"], tracks=[]
    )
    return store.get_bucket(name)
