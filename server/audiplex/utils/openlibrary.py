"""OpenLibrary series enrichment — one-time bulk lookup for books missing series info."""

import asyncio
import logging
from urllib.parse import quote_plus

import httpx

logger = logging.getLogger(__name__)

_BASE = "https://openlibrary.org"
_RATE_LIMIT = 1.0  # seconds between requests


async def lookup_series(
    title: str, author: str | None, client: httpx.AsyncClient
) -> tuple[str | None, str | None]:
    """Query OpenLibrary by title+author, return (series_name, sequence) or (None, None)."""
    params = {"title": title, "fields": "key,title,subject,edition_count", "limit": "3"}
    if author:
        params["author"] = author

    try:
        resp = await client.get(f"{_BASE}/search.json", params=params, timeout=10.0)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.debug(f"OpenLibrary search failed for '{title}': {e}")
        return None, None

    docs = data.get("docs", [])
    if not docs:
        return None, None

    # Try the top result's work page for series info
    work_key = docs[0].get("key")
    if not work_key:
        return None, None

    try:
        await asyncio.sleep(_RATE_LIMIT)
        resp = await client.get(f"{_BASE}{work_key}.json", timeout=10.0)
        resp.raise_for_status()
        work = resp.json()
    except Exception as e:
        logger.debug(f"OpenLibrary work fetch failed for {work_key}: {e}")
        return None, None

    # Check subjects for series patterns like "series:Name of Series"
    subjects = work.get("subjects", [])
    for subj in subjects:
        if isinstance(subj, str) and subj.lower().startswith("series:"):
            series_name = subj[7:].strip()
            if series_name:
                return series_name, None

    # Check for explicit series field (less common but exists)
    series_list = work.get("series", [])
    if series_list:
        entry = series_list[0] if isinstance(series_list, list) else series_list
        if isinstance(entry, str):
            return entry, None

    # Check subject_people and subject_places are not useful — skip
    # Look in subjects for "Book N" pattern as a last resort
    for subj in subjects:
        if isinstance(subj, str) and "book" in subj.lower():
            import re
            m = re.search(r"(.+?),?\s*book\s+(\d+)", subj, re.IGNORECASE)
            if m:
                return m.group(1).strip(), m.group(2)

    return None, None


async def enrich_books(
    books: list[dict],
) -> list[dict]:
    """Enrich a batch of books with OpenLibrary series info.

    Each entry in books should have 'id', 'title', 'author'.
    Returns list of dicts with 'id', 'series', 'sequence' for books that got results.
    """
    results = []
    async with httpx.AsyncClient(
        headers={"User-Agent": "Audiplex/0.1 (audiobook server; contact: audiplex@localhost)"}
    ) as client:
        for book in books:
            series, sequence = await lookup_series(
                book["title"], book.get("author"), client
            )
            if series:
                results.append({
                    "id": book["id"],
                    "series": series,
                    "sequence": sequence,
                })
            await asyncio.sleep(_RATE_LIMIT)
    return results
