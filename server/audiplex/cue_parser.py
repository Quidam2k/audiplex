"""Parse CUE files as exported by Libation for audiobooks."""

import re
from dataclasses import dataclass


@dataclass
class CueChapter:
    index: int
    title: str
    start_seconds: float


def parse_timestamp(timestamp: str) -> float:
    """Parse MM:SS:FF timestamp where FF = frames at 75fps.

    Returns seconds as a float.
    """
    match = re.match(r"(\d+):(\d+):(\d+)", timestamp.strip())
    if not match:
        raise ValueError(f"Invalid CUE timestamp: {timestamp!r}")
    minutes, seconds, frames = int(match.group(1)), int(match.group(2)), int(match.group(3))
    return minutes * 60.0 + seconds + frames / 75.0


def parse_cue_file(cue_path: str) -> list[CueChapter]:
    """Parse a CUE file and return a list of chapters.

    Libation uses TRACK/TITLE/INDEX 01 format.
    The FILE type may say MP3 even for M4B — we ignore it.
    """
    with open(cue_path, encoding="utf-8-sig") as f:
        content = f.read()

    chapters: list[CueChapter] = []
    current_title: str | None = None
    track_index = 0

    for line in content.splitlines():
        line = line.strip()

        # Skip empty lines and REM comments
        if not line or line.startswith("REM"):
            continue

        # Track title
        title_match = re.match(r'TITLE\s+"(.+)"', line)
        if title_match:
            # Only capture TITLE lines that appear after a TRACK line
            # (the first TITLE before any TRACK is the album title)
            if track_index > 0 or "TRACK" in content.split(line)[0]:
                current_title = title_match.group(1)
            else:
                # Album-level title, skip — but set it as default
                current_title = title_match.group(1)
            continue

        # Track declaration
        track_match = re.match(r"TRACK\s+(\d+)\s+\w+", line)
        if track_match:
            track_index = int(track_match.group(1))
            current_title = None  # Reset for this track's TITLE
            continue

        # Index point (we only care about INDEX 01, the start)
        index_match = re.match(r"INDEX\s+01\s+(\d+:\d+:\d+)", line)
        if index_match and track_index > 0:
            start = parse_timestamp(index_match.group(1))
            title = current_title or f"Chapter {track_index}"
            chapters.append(CueChapter(
                index=track_index - 1,  # 0-based
                title=title,
                start_seconds=start,
            ))
            continue

    return chapters
