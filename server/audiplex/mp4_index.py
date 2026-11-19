"""#4217: where an MP4/M4B keeps its moov (seek index) atom.

Every Libation book keeps moov at the END of the file (median 5 MB), so a phone
resuming a book first downloads that range. The client prefetches it into its
book cache; this tells it which bytes to fetch without probing.
"""

import os
import struct

MP4_SUFFIXES = (".m4b", ".m4a", ".mp4")


def find_moov(path: str) -> tuple[int, int] | None:
    """(offset, size) of the top-level moov atom, or None if absent or unreadable."""
    try:
        total = os.path.getsize(path)
        with open(path, "rb") as f:
            pos = 0
            while pos + 8 <= total:
                f.seek(pos)
                head = f.read(8)
                if len(head) < 8:
                    return None
                size, kind = struct.unpack(">I4s", head)
                if size == 1:  # 64-bit size follows the type
                    ext = f.read(8)
                    if len(ext) < 8:
                        return None
                    size = struct.unpack(">Q", ext)[0]
                elif size == 0:  # atom runs to end of file
                    size = total - pos
                if size < 8 or pos + size > total:
                    return None
                if kind == b"moov":
                    return pos, size
                pos += size
    except OSError:
        return None
    return None
