"""Extract metadata from M4B/M4A files using mutagen."""

import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from mutagen.mp4 import MP4


_AUTHOR_SPLIT = re.compile(r"\s*[;,&]\s*|\s+(?:and|with)\s+", re.IGNORECASE)


def normalize_author(value: str | None) -> str | None:
    """Pick the primary author from a multi-author string.

    Libation tends to write the primary author first. Split on `,`, `;`, `&`,
    or " and "/" with " and keep the first token so co-author rolls and
    "Author, Translator" tags collapse into the primary author's group.
    """
    if not value:
        return None
    primary = _AUTHOR_SPLIT.split(value, maxsplit=1)[0].strip()
    return primary or None


def normalize_series(value: str | None) -> str | None:
    """Strip and collapse internal whitespace so cosmetic variants group together."""
    if not value:
        return None
    collapsed = re.sub(r"\s+", " ", value).strip()
    return collapsed or None


# Libation packs the audiobook's full Audible album title into the M4B
# album tag, e.g. "Caliban's War: The Expanse, Book 2 (Unabridged)".
# The actual series name is the part after the colon and before ", Book N".
# Some titles use a parenthesized form: "A Deadly Education: A Novel
# (The Scholomance, Book 1) (Unabridged)".
_SERIES_PAREN = re.compile(
    r"\(([^()]+?),\s*Book\s+(\d+(?:\.\d+)?)\)",
    re.IGNORECASE,
)
_SERIES_COLON = re.compile(
    # Greedy `.*` anchors to the LAST colon so titles that themselves contain
    # colons (e.g. "Mistborn: Secret History: Mistborn, Book 3.5") still
    # surface "Mistborn" as the series rather than "Secret History: Mistborn".
    r"^.*:\s*(.+?),\s*Book\s+(\d+(?:\.\d+)?)(?:\s*\([^)]*\))?\s*$",
    re.IGNORECASE,
)

# "#N" after colon: "Curse of the Blue Tattoo: Bloody Jack #2 (Unabridged)"
_SERIES_HASH = re.compile(
    r"^.*:\s*(.+?)\s+#(\d+(?:\.\d+)?)(?:\s*\([^)]*\))?\s*$",
    re.IGNORECASE,
)

# "Volume N": "Dragonsong: Harper Hall Trilogy, Volume 1 (Unabridged)"
_SERIES_VOLUME = re.compile(
    r"^.*:\s*(.+?),\s*Volume\s+(\d+(?:\.\d+)?)(?:\s*\([^)]*\))?\s*$",
    re.IGNORECASE,
)

# Roman numeral "Book I of": "Hard Magic: Book I of the Grimnoir Chronicles (Unabridged)"
_SERIES_ROMAN = re.compile(
    r"Book\s+([IVXivx]+)\s+of\s+(?:the\s+)?(.+?)(?:\s*\(|$)",
    re.IGNORECASE,
)

# Word number "Book One of": "Quicksilver: Book One of The Baroque Cycle (Unabridged)"
_SERIES_WORD = re.compile(
    r"Book\s+(One|Two|Three|Four|Five|Six|Seven|Eight|Nine|Ten|Eleven|Twelve)\s+of\s+(?:the\s+)?(.+?)(?:\s*\(|$)",
    re.IGNORECASE,
)

# "Series Book N" (no comma): "The Final Empire: Mistborn Book 1 (Unabridged)"
_SERIES_BOOK_N = re.compile(
    r"^.*:\s*(.+?)\s+Book\s+(\d+(?:\.\d+)?)(?:\s*\([^)]*\))?\s*$",
    re.IGNORECASE,
)


_ROMAN_MAP = {"I": 1, "V": 5, "X": 10}


def _roman_to_int(s: str) -> int:
    s = s.upper()
    total = 0
    prev = 0
    for ch in reversed(s):
        val = _ROMAN_MAP.get(ch, 0)
        if val < prev:
            total -= val
        else:
            total += val
        prev = val
    return total


_WORD_MAP = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12,
}


def _word_to_int(s: str) -> int:
    return _WORD_MAP.get(s.lower(), 0)


def parse_series_from_album(album_tag: str | None) -> tuple[str | None, str | None]:
    """Pull (series_name, book_number) out of a Libation album tag.

    Returns (None, None) when the tag doesn't carry recognizable series info
    so standalone books don't pollute the series list as 1-book groups.
    """
    if not album_tag:
        return None, None
    text = album_tag.strip()

    # Parenthesized form first — it's nested and would otherwise be partially
    # swallowed by the colon form.
    m = _SERIES_PAREN.search(text)
    if m:
        series = normalize_series(m.group(1))
        sequence = m.group(2)
        if series:
            return series, sequence

    m = _SERIES_COLON.search(text)
    if m:
        series = normalize_series(m.group(1))
        sequence = m.group(2)
        if series:
            return series, sequence

    # "#N" after colon
    m = _SERIES_HASH.search(text)
    if m:
        series = normalize_series(m.group(1))
        sequence = m.group(2)
        if series:
            return series, sequence

    # "Volume N"
    m = _SERIES_VOLUME.search(text)
    if m:
        series = normalize_series(m.group(1))
        sequence = m.group(2)
        if series:
            return series, sequence

    # Roman numeral "Book I of the Series"
    m = _SERIES_ROMAN.search(text)
    if m:
        series = normalize_series(m.group(2))
        sequence = str(_roman_to_int(m.group(1)))
        if series and sequence != "0":
            return series, sequence

    # Word number "Book One of the Series"
    m = _SERIES_WORD.search(text)
    if m:
        series = normalize_series(m.group(2))
        sequence = str(_word_to_int(m.group(1)))
        if series and sequence != "0":
            return series, sequence

    # "Series Book N" (no comma)
    m = _SERIES_BOOK_N.search(text)
    if m:
        series = normalize_series(m.group(1))
        sequence = m.group(2)
        if series:
            return series, sequence

    return None, None


_OVERRIDES_PATH = Path(__file__).resolve().parent.parent / "series_overrides.yaml"


def load_overrides() -> list[dict]:
    """Load manual series overrides from YAML file."""
    if not _OVERRIDES_PATH.exists():
        return []
    with open(_OVERRIDES_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not data or "overrides" not in data:
        return []
    return data["overrides"]


def apply_override(
    title: str, author: str | None, overrides: list[dict]
) -> tuple[str | None, str | None]:
    """Match a book against overrides by title (case-insensitive) and optional author."""
    title_lower = title.lower()
    for entry in overrides:
        if entry.get("title", "").lower() != title_lower:
            continue
        entry_author = entry.get("author")
        if entry_author and author and entry_author.lower() != author.lower():
            continue
        return entry.get("series"), entry.get("sequence")
    return None, None


@dataclass
class BookMetadata:
    title: str
    author: str | None = None
    narrator: str | None = None
    series: str | None = None
    series_sequence: str | None = None
    series_raw: str | None = None
    duration_seconds: float = 0.0
    file_size: int = 0
    file_hash: str = ""
    has_cover: bool = False
    embedded_chapters: list[dict] = field(default_factory=list)


def compute_file_hash(file_path: str) -> str:
    """Fast hash based on file size + mtime (not content — files can be huge)."""
    stat = os.stat(file_path)
    data = f"{stat.st_size}:{stat.st_mtime}".encode()
    return hashlib.md5(data).hexdigest()


def _get_tag(mp4: MP4, key: str) -> str | None:
    """Get a single string value from an MP4 tag."""
    val = mp4.tags.get(key) if mp4.tags else None
    if val and len(val) > 0:
        return str(val[0])
    return None


def _extract_series_info(mp4: MP4) -> tuple[str | None, str | None]:
    """Try to extract series name and sequence number.

    Libation stores series info in different ways:
    - \xa9alb (album) often contains the series name
    - Custom tags may have sequence info
    """
    series = _get_tag(mp4, "\xa9alb")

    # Try to extract sequence from sort order or description tags
    sequence = None
    # Check for track number as sequence
    trkn = mp4.tags.get("trkn") if mp4.tags else None
    if trkn and len(trkn) > 0:
        try:
            sequence = str(trkn[0][0])
        except (IndexError, TypeError):
            pass

    return series, sequence


def extract_metadata(file_path: str) -> BookMetadata:
    """Extract audiobook metadata from an M4B/M4A file."""
    path = Path(file_path)
    mp4 = MP4(file_path)

    title = _get_tag(mp4, "\xa9nam") or path.stem
    author = normalize_author(_get_tag(mp4, "\xa9ART"))
    narrator = _get_tag(mp4, "aART") or _get_tag(mp4, "\xa9wrt")
    raw_album, embedded_sequence = _extract_series_info(mp4)
    series_raw = normalize_series(raw_album)
    parsed_series, parsed_sequence = parse_series_from_album(series_raw)
    series = parsed_series
    series_sequence = parsed_sequence or embedded_sequence

    duration = mp4.info.length if mp4.info else 0.0
    file_size = os.path.getsize(file_path)
    file_hash = compute_file_hash(file_path)

    has_cover = bool(mp4.tags and mp4.tags.get("covr"))

    # Try to extract embedded chapters
    embedded_chapters = []
    if hasattr(mp4, "chapters") and mp4.chapters:
        for i, ch in enumerate(mp4.chapters):
            embedded_chapters.append({
                "index": i,
                "title": ch.title or f"Chapter {i + 1}",
                "start_seconds": ch.start / 1000.0,
            })

    return BookMetadata(
        title=title,
        author=author,
        narrator=narrator,
        series=series,
        series_sequence=series_sequence,
        series_raw=series_raw,
        duration_seconds=duration,
        file_size=file_size,
        file_hash=file_hash,
        has_cover=has_cover,
        embedded_chapters=embedded_chapters,
    )
