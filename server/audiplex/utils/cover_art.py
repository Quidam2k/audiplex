"""Cover art extraction + caching.

The audiobook (M4B) flow uses `extract_cover_art` + `get_cover_path` keyed by
`book_id`. The misc-audiobook + music flows use `extract_embedded_cover` +
`find_filesystem_cover` + `save_cover`, with album-keyed lookup via
`get_album_cover_path`.
"""

from pathlib import Path

import mutagen
from mutagen.mp4 import MP4


def _sniff_image_ext(data: bytes) -> str:
    """Pick a file extension based on image magic bytes; default 'jpg'."""
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    return "jpg"


def extract_embedded_cover(track_path: Path) -> tuple[bytes, str] | None:
    """Pull embedded cover art from a track. Returns (bytes, ext) or None.

    Handles MP4 `covr`, ID3 `APIC`, FLAC `pictures`, and OGG
    `metadata_block_picture` (base64-encoded FLAC Picture block).
    """
    try:
        audio = mutagen.File(str(track_path))
    except Exception:
        return None
    if audio is None:
        return None

    # MP4 (m4a / m4b)
    try:
        covers = audio.tags.get("covr") if audio.tags else None
        if covers:
            data = bytes(covers[0])
            return data, _sniff_image_ext(data)
    except Exception:
        pass

    # ID3 (mp3) — APIC frames
    try:
        for key in audio.tags or {}:
            if key.startswith("APIC"):
                frame = audio.tags[key]
                data = frame.data
                return data, _sniff_image_ext(data)
    except Exception:
        pass

    # FLAC pictures
    try:
        pictures = getattr(audio, "pictures", None)
        if pictures:
            pic = pictures[0]
            return pic.data, _sniff_image_ext(pic.data)
    except Exception:
        pass

    # OGG Vorbis — base64-encoded FLAC Picture block in metadata_block_picture
    try:
        if audio.tags and "metadata_block_picture" in audio.tags:
            import base64
            from mutagen.flac import Picture
            raw = audio.tags["metadata_block_picture"]
            value = raw[0] if isinstance(raw, list) else raw
            pic = Picture(base64.b64decode(value))
            return pic.data, _sniff_image_ext(pic.data)
    except Exception:
        pass

    return None


def find_filesystem_cover(folder: Path, names: tuple[str, ...]) -> Path | None:
    """Look for a sidecar cover image in `folder`.

    `names` is checked in order. After exact-name lookups, try a
    case-insensitive `AlbumArt_*_Large.jpg` glob (Windows Media Player
    leftover), then any `*.jpg` in the folder.
    """
    for name in names:
        candidate = folder / name
        if candidate.exists():
            return candidate

    try:
        for entry in folder.iterdir():
            if entry.is_file():
                lower = entry.name.lower()
                if lower.startswith("albumart_") and lower.endswith("_large.jpg"):
                    return entry
    except (OSError, PermissionError):
        return None

    try:
        for entry in folder.iterdir():
            if entry.is_file() and entry.suffix.lower() == ".jpg":
                return entry
    except (OSError, PermissionError):
        return None

    return None


def save_cover(key: str, data: bytes, ext: str, cache_dir: str) -> None:
    """Write cover bytes to `cache_dir/{key}.{ext}`."""
    cache_path = Path(cache_dir)
    cache_path.mkdir(parents=True, exist_ok=True)
    (cache_path / f"{key}.{ext}").write_bytes(data)


def extract_cover_art(file_path: str, book_id: int, cache_dir: str) -> str | None:
    """Extract cover art from an M4B file and save to disk (book-keyed).

    Returns the path to the saved cover image, or None if no cover art found.
    """
    mp4 = MP4(file_path)
    if not mp4.tags:
        return None

    covers = mp4.tags.get("covr")
    if not covers:
        return None

    cover_data = bytes(covers[0])
    ext = _sniff_image_ext(cover_data)

    cache_path = Path(cache_dir)
    cache_path.mkdir(parents=True, exist_ok=True)
    cover_file = cache_path / f"{book_id}.{ext}"
    cover_file.write_bytes(cover_data)
    return str(cover_file)


def get_cover_path(book_id: int, cache_dir: str) -> str | None:
    """Find cached cover art for a book."""
    cache_path = Path(cache_dir)
    for ext in ("jpg", "png"):
        candidate = cache_path / f"{book_id}.{ext}"
        if candidate.exists():
            return str(candidate)
    return None


def get_album_cover_path(album_id: int, cache_dir: str) -> str | None:
    """Find cached cover art for an album (keyed `album_{id}.{ext}`)."""
    cache_path = Path(cache_dir)
    for ext in ("jpg", "png"):
        candidate = cache_path / f"album_{album_id}.{ext}"
        if candidate.exists():
            return str(candidate)
    return None
