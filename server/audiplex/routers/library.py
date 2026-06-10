"""Library API — browse books, authors, series, covers, trigger scans."""

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from audiplex.auth import get_admin_user, get_current_user
from audiplex.config import get_settings
from audiplex.database import get_db
from audiplex.models import Book, User
from audiplex.scanner import scan_library
from audiplex.schemas import (
    AuthorSchema,
    BookDetail,
    BookSummary,
    ScanResultSchema,
    SeriesSchema,
)
from audiplex.utils.cover_art import get_cover_path
from audiplex.utils.openlibrary import enrich_books

router = APIRouter(prefix="/api/library", tags=["library"])


@router.get("/books", response_model=list[BookSummary])
def list_books(
    author: str | None = Query(None),
    series: str | None = Query(None),
    category: str | None = Query(None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    query = db.query(Book)
    if author:
        query = query.filter(Book.author == author)
    if series:
        query = query.filter(Book.series == series)
    if category:
        query = query.filter(Book.category == category)
    return query.order_by(Book.title).all()


@router.get("/books/{book_id}", response_model=BookDetail)
def get_book(book_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    book = db.query(Book).filter(Book.id == book_id).first()
    if not book:
        raise HTTPException(status_code=404, detail="Book not found")
    detail = BookDetail.model_validate(book)
    if book.category == "audiobook_misc":
        detail.track_urls = [
            f"/api/stream/{book.id}/track/{ch.index}"
            for ch in book.chapters
            if ch.file_path
        ]
    return detail


@router.get("/books/{book_id}/cover")
def get_cover(book_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    book = db.query(Book).filter(Book.id == book_id).first()
    if not book:
        raise HTTPException(status_code=404, detail="Book not found")

    settings = get_settings()
    cover_path = get_cover_path(book_id, settings.cover_cache_dir)
    if not cover_path:
        raise HTTPException(status_code=404, detail="No cover art available")

    media_type = "image/jpeg" if cover_path.endswith(".jpg") else "image/png"
    return FileResponse(cover_path, media_type=media_type)


@router.get("/authors", response_model=list[AuthorSchema])
def list_authors(
    category: str | None = Query(None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    query = db.query(Book.author, func.count(Book.id)).filter(Book.author.is_not(None))
    if category:
        query = query.filter(Book.category == category)
    results = query.group_by(Book.author).order_by(Book.author).all()
    return [AuthorSchema(name=name, book_count=count) for name, count in results]


@router.get("/series", response_model=list[SeriesSchema])
def list_series(
    category: str | None = Query(None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    query = db.query(Book.series, func.count(Book.id)).filter(Book.series.is_not(None))
    if category:
        query = query.filter(Book.category == category)
    results = query.group_by(Book.series).order_by(Book.series).all()
    return [SeriesSchema(name=name, book_count=count) for name, count in results]


@router.post("/scan", response_model=ScanResultSchema)
def trigger_scan(db: Session = Depends(get_db), user: User = Depends(get_admin_user)):
    settings = get_settings()
    return scan_library(db, settings.library_roots, settings.cover_cache_dir)


class EnrichResultSchema(BaseModel):
    checked: int
    enriched: int


@router.post("/enrich-series", response_model=EnrichResultSchema)
async def enrich_series(db: Session = Depends(get_db), user: User = Depends(get_admin_user)):
    """One-time bulk OpenLibrary lookup for books missing series info."""
    candidates = (
        db.query(Book)
        .filter(Book.series.is_(None), Book.series_raw.is_not(None))
        .filter(Book.category == "audiobook_clean")
        .all()
    )
    if not candidates:
        return EnrichResultSchema(checked=0, enriched=0)

    book_list = [
        {"id": b.id, "title": b.title, "author": b.author}
        for b in candidates
    ]
    results = await enrich_books(book_list)

    for r in results:
        book = db.query(Book).filter(Book.id == r["id"]).first()
        if book:
            book.series = r["series"]
            if r.get("sequence"):
                book.series_sequence = r["sequence"]
            book.series_source = "openlibrary"

    db.commit()
    return EnrichResultSchema(checked=len(candidates), enriched=len(results))
