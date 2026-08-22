"""Audio streaming with HTTP range request support."""

import os

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from audiplex.auth import get_current_user
from audiplex.config import get_settings  # #1001
from audiplex.database import get_db
from audiplex.models import Book, Chapter, User
from audiplex.utils.streaming import EXT_MIME, serve_file, storage_root_offline  # #1001

router = APIRouter(prefix="/api/stream", tags=["streaming"])


def _missing_file_error(file_path: str) -> HTTPException:
    """404 for a genuinely-deleted file, but 503 when the file's whole library
    root is unreachable (SMB/USB offline) — retryable vs permanent, so Media3
    retries and the app can say "storage offline" instead of failing hard. #1001
    """
    roots = [r.path for r in get_settings().library_roots]
    if storage_root_offline(file_path, roots):
        return HTTPException(
            status_code=503,
            detail="Library storage offline; the file's drive is unreachable. Retry shortly.",
        )
    return HTTPException(status_code=404, detail="Audio file not found on disk")


@router.get("/{book_id}")
def stream_audio(book_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    book = db.query(Book).filter(Book.id == book_id).first()
    if not book:
        raise HTTPException(status_code=404, detail="Book not found")

    if book.category == "audiobook_misc":
        raise HTTPException(
            status_code=404,
            detail="Misc books stream per-track; use /api/stream/{book_id}/track/{track_index}",
        )

    file_path = book.file_path
    if not os.path.exists(file_path):
        raise _missing_file_error(file_path)  # #1001

    ext = os.path.splitext(file_path)[1].lower()
    return serve_file(file_path, EXT_MIME.get(ext, "audio/mp4"), request)


@router.get("/{book_id}/track/{track_index}")
def stream_track(
    book_id: int,
    track_index: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    book = db.query(Book).filter(Book.id == book_id).first()
    if not book or book.category != "audiobook_misc":
        raise HTTPException(status_code=404, detail="Track stream not available")

    chapter = (
        db.query(Chapter)
        .filter(Chapter.book_id == book_id, Chapter.index == track_index)
        .first()
    )
    if not chapter or not chapter.file_path:  # #1001
        raise HTTPException(status_code=404, detail="Track file not found")
    if not os.path.exists(chapter.file_path):  # #1001
        raise _missing_file_error(chapter.file_path)  # #1001

    ext = os.path.splitext(chapter.file_path)[1].lower()
    content_type = EXT_MIME.get(ext, "application/octet-stream")
    return serve_file(chapter.file_path, content_type, request)
