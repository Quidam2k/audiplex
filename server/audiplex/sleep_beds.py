"""Sleep beds (#3714): the looping tracks sleep mode fades a book into.

Found by title in the library (any category), so dropping a new track into a
library root and rescanning adds it with no config edit. Order is preference:
the first match is the default bed. audiplex_mcp/server.py keeps a copy of
this list for dj_sleep_start (#3367/#5889); keep the two in step.
"""

from sqlalchemy.orm import Session

from audiplex.models import Book

SLEEP_BED_TITLES = (
    "Star Ship Sleeping Quarters",
    "Starship Sleeping Quarters",
    "Sleeping Quarters",
    "Brown Noise - Sleep Loop",
)


def find_beds(db: Session) -> list[Book]:
    """Library books that match a bed title, default first, each book once."""
    beds: list[Book] = []
    books = db.query(Book).order_by(Book.title).all()
    for want in SLEEP_BED_TITLES:
        for b in books:
            if want.lower() in (b.title or "").lower() and b not in beds:
                beds.append(b)
    return beds
