"""Playback progress tracking — position sync between client and server."""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from audiplex.auth import get_current_user
from audiplex.database import get_db
from audiplex.models import Book, Favorite, PlaybackPosition, User
from audiplex.schemas import ProgressSchema, ProgressUpdate

router = APIRouter(prefix="/api/progress", tags=["progress"])


def _naive_utc(ts: datetime | None) -> datetime | None:
    """SQLite hands back naive UTC; clients send aware times. Compare as naive UTC."""
    if ts is None or ts.tzinfo is None:
        return ts
    return ts.astimezone(timezone.utc).replace(tzinfo=None)


@router.get("", response_model=list[ProgressSchema])
def list_progress(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Get all in-progress books (for 'continue listening')."""
    positions = (
        db.query(PlaybackPosition)
        .filter(
            PlaybackPosition.user_id == user.id,
            PlaybackPosition.is_finished == False,  # noqa: E712
        )
        .order_by(PlaybackPosition.updated_at.desc())
        .all()
    )
    return positions


@router.get("/{book_id}", response_model=ProgressSchema)
def get_progress(book_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    position = db.query(PlaybackPosition).filter(
        PlaybackPosition.book_id == book_id,
        PlaybackPosition.user_id == user.id,
    ).first()
    if not position:
        raise HTTPException(status_code=404, detail="No progress found for this book")
    return position


@router.put("/{book_id}", response_model=ProgressSchema)
def update_progress(
    book_id: int,
    update: ProgressUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    book = db.query(Book).filter(Book.id == book_id).first()
    if not book:
        raise HTTPException(status_code=404, detail="Book not found")

    position = db.query(PlaybackPosition).filter(
        PlaybackPosition.book_id == book_id,
        PlaybackPosition.user_id == user.id,
    ).first()

    if position and update.client_updated_at is not None:
        # #2680: a position sampled before the stored one was written is stale
        # (e.g. the PC's last push arriving after the phone moved on). Keep the
        # newer one and tell the client which it is.
        stored = _naive_utc(position.updated_at)
        if stored is not None and _naive_utc(update.client_updated_at) < stored:
            raise HTTPException(
                status_code=409,
                detail={
                    "reason": "stale",
                    "position_seconds": position.position_seconds,
                    "updated_at": stored.isoformat() + "Z",
                },
            )

    if position:
        position.position_seconds = update.position_seconds
        position.chapter_index = update.chapter_index
        position.is_finished = update.is_finished
        position.updated_at = datetime.now(timezone.utc)
    else:
        position = PlaybackPosition(
            book_id=book_id,
            user_id=user.id,
            position_seconds=update.position_seconds,
            chapter_index=update.chapter_index,
            is_finished=update.is_finished,
        )
        db.add(position)

    if update.is_finished:
        db.query(Favorite).filter(
            Favorite.entity_type == "to_read",
            Favorite.entity_key == str(book_id),
            Favorite.user_id == user.id,
        ).delete()

    db.commit()
    db.refresh(position)
    return position
