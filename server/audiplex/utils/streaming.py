"""HTTP range-request primitives for audio streaming.

Shared by `routers/streaming.py` (audiobook streams) and `routers/music.py`
(music track streams). Behavior is identical to the previously inlined helpers
in `routers/streaming.py`.
"""

import os
import re
from typing import Generator

from fastapi import Request
from fastapi.responses import Response, StreamingResponse

CHUNK_SIZE = 64 * 1024  # 64KB

EXT_MIME = {
    ".mp3": "audio/mpeg",
    ".mpeg": "audio/mpeg",
    ".mp2": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".m4b": "audio/mp4",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".wav": "audio/wav",
}


def _norm_path(p: str) -> str:
    """Fold a path for ancestor comparison: forward slashes, no trailing slash,
    lowercased. Windows paths are case-insensitive and mix separators, and both
    UNC (\\\\host\\share) and drive-letter roots appear here. #1001"""
    return p.replace("\\", "/").rstrip("/").lower()


def storage_root_offline(file_path: str, library_root_paths: list[str]) -> bool:
    """True when `file_path` sits under a configured library root that is itself
    currently unreachable on disk — the STORAGE is offline, not the file deleted.

    Lets a missing file be answered with 503 (retryable) instead of 404
    (permanent). When a whole SMB/USB root drops, every file under it vanishes
    at once; Media3 treats 404 as a hard source error but will retry a 503, so
    playback self-heals once the share returns. A path under a root that DOES
    exist (or under no configured root) is a genuine 404, so this returns False.
    #1001 — the Athena `\\\\Athena.local\\...\\Libation\\Books` share dropping
    404'd all 598 audiobooks on it, and the phone showed a permanent stall.
    """
    target = _norm_path(file_path)
    for root in library_root_paths:
        r = _norm_path(root)
        if not r:
            continue
        if target == r or target.startswith(r + "/"):
            # Match the original (unnormalized) root against the filesystem —
            # os.path.exists is itself case/separator tolerant on Windows.
            if not os.path.exists(root):
                return True
    return False


def parse_range_header(range_header: str, file_size: int) -> tuple[int, int]:
    """Parse Range header, return (start, end) byte positions.

    Supports: bytes=0-1023, bytes=1024-, bytes=-512
    """
    match = re.match(r"bytes=(\d*)-(\d*)", range_header)
    if not match:
        raise ValueError("Invalid range format")

    start_str, end_str = match.group(1), match.group(2)

    if start_str and end_str:
        start, end = int(start_str), int(end_str)
    elif start_str:
        start = int(start_str)
        end = file_size - 1
    elif end_str:
        # bytes=-N means last N bytes
        start = file_size - int(end_str)
        end = file_size - 1
    else:
        raise ValueError("Invalid range format")

    if start < 0 or end >= file_size or start > end:
        raise ValueError(f"Range not satisfiable: {start}-{end}/{file_size}")

    return start, end


def stream_file(file_path: str, start: int, end: int) -> Generator[bytes, None, None]:
    """Stream file bytes in chunks."""
    with open(file_path, "rb") as f:
        f.seek(start)
        remaining = end - start + 1
        while remaining > 0:
            chunk_size = min(CHUNK_SIZE, remaining)
            data = f.read(chunk_size)
            if not data:
                break
            remaining -= len(data)
            yield data


def serve_file(file_path: str, content_type: str, request: Request):
    file_size = os.path.getsize(file_path)
    range_header = request.headers.get("range")

    if not range_header:
        return StreamingResponse(
            stream_file(file_path, 0, file_size - 1),
            media_type=content_type,
            headers={
                "Content-Length": str(file_size),
                "Accept-Ranges": "bytes",
            },
        )

    try:
        start, end = parse_range_header(range_header, file_size)
    except ValueError:
        return Response(
            status_code=416,
            headers={"Content-Range": f"bytes */{file_size}"},
        )

    content_length = end - start + 1
    return StreamingResponse(
        stream_file(file_path, start, end),
        status_code=206,
        media_type=content_type,
        headers={
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Content-Length": str(content_length),
            "Accept-Ranges": "bytes",
        },
    )
