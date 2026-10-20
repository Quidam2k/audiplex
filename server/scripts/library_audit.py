"""Audit the Audiplex library and produce a Markdown pick-list and JSON report.

#3385: Todd asked to "clean up and tidy" the library. This lists what looks off
(duplicates, missing files, uncatalogued audio, missing covers/chapters, stray
files, empty folders) so he can pick. Library files and the DB are only read;
reports are refused inside a library root.

Usage:
    python scripts/library_audit.py
    python scripts/library_audit.py --top 25
    python scripts/library_audit.py --db audiplex.db --config config.yaml \
        --out-md library-audit.md --out-json library-audit.json

Python 3.11 or newer is required. PyYAML is optional; a small parser handles
library_roots entries containing category/path pairs when it is unavailable.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import unicodedata


SERVER = Path(__file__).resolve().parents[1]

AUDIO_EXTENSIONS = {
    ".m4b", ".m4a", ".mp3", ".flac", ".ogg", ".opus", ".wav", ".aac", ".wma",
}
SIDECAR_EXTENSIONS = {
    ".cue", ".jpg", ".jpeg", ".png", ".webp", ".nfo", ".txt", ".pdf",
    ".json", ".m3u", ".m3u8", ".log", ".lrc", ".srt", ".vtt",
}
SIDECAR_NAMES = {"desktop.ini", "thumbs.db", ".ds_store", "folder.jpg"}

SECTION_NAMES = {
    "duplicate_files": "Files that look duplicated",
    "duplicate_books": "Books with the same title and author",
    "missing_files": "Library entries with missing files",
    "orphan_audio": "Audio outside the library catalogue",
    "books_missing_metadata": "Books with missing details",
    "albums_missing_cover": "Albums missing cover art",
    "stray_files": "Unexpected file types",
    "empty_folders": "Empty folders",
}


def normalize_path(path: str | Path | None) -> str:
    """Compare Windows paths consistently, including when tests run elsewhere."""
    if path is None or str(path) == "":
        return ""
    value = os.path.normcase(os.path.normpath(str(path).replace("\\", "/")))
    return value.replace("\\", "/").casefold()


def normalize_title(value: str | None) -> str:
    value = unicodedata.normalize("NFKD", value or "").lower()
    value = "".join(char for char in value if char.isalnum() or char.isspace())
    value = re.sub(r"\s+", " ", value).strip()
    return value[4:] if value.startswith("the ") else value


def _under_path(path: str, root: str) -> bool:
    return bool(path and root) and (
        path == root or path.startswith(root.rstrip("/") + "/")
    )


def _reachable_path(path: str | None, roots: list[dict]) -> bool:
    """Use the most specific configured root for overlapping root paths."""
    normalized = normalize_path(path)
    matches = [
        root for root in roots
        if _under_path(normalized, normalize_path(root["path"]))
    ]
    if not matches:
        return False
    root = max(matches, key=lambda item: len(normalize_path(item["path"])))
    return bool(root.get("reachable", False))


def _size(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _yaml_scalar(value: str) -> str:
    """Read a quoted or plain scalar, allowing a trailing YAML comment."""
    value = value.strip()
    if value.startswith("'"):
        match = re.match(r"^'((?:''|[^'])*)'\s*(?:#.*)?$", value)
        if not match:
            raise ValueError(f"Invalid quoted config value: {value}")
        return match.group(1).replace("''", "'")
    if value.startswith('"'):
        match = re.match(r'^"((?:\\.|[^"\\])*)"\s*(?:#.*)?$', value)
        if not match:
            raise ValueError(f"Invalid quoted config value: {value}")
        try:
            return json.loads('"' + match.group(1) + '"')
        except ValueError as exc:
            raise ValueError(f"Invalid quoted config value: {value}") from exc
    return re.split(r"\s+#", value, maxsplit=1)[0].strip()


def _parse_roots_without_yaml(text: str) -> list[dict]:
    roots = []
    current = None
    section_indent = None

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        if section_indent is None:
            match = re.match(r"^(\s*)library_roots\s*:\s*(.*?)\s*$", line)
            if not match:
                continue
            remainder = match.group(2).split("#", 1)[0].strip()
            if remainder == "[]":
                return []
            if remainder:
                raise ValueError(
                    "Without PyYAML, library_roots must use category/path pairs."
                )
            section_indent = len(match.group(1))
            continue

        indent = len(line) - len(line.lstrip())
        is_item = stripped.startswith("- ")
        if indent <= section_indent and not is_item:
            break

        if is_item:
            if current is not None:
                roots.append(current)
            current = {}
            stripped = stripped[2:].strip()

        if current is None:
            continue
        match = re.match(r"^(path|category)\s*:\s*(.*?)\s*$", stripped)
        if match:
            current[match.group(1)] = _yaml_scalar(match.group(2))

    if current is not None:
        roots.append(current)
    return roots


def load_roots(config_path: str | Path) -> list[dict]:
    text = Path(config_path).read_text(encoding="utf-8-sig")
    try:
        import yaml
    except ImportError:
        configured = _parse_roots_without_yaml(text)
    else:
        try:
            config = yaml.safe_load(text) or {}
        except yaml.YAMLError as exc:
            raise ValueError(f"Cannot parse configuration: {exc}") from exc
        if not isinstance(config, dict):
            raise ValueError("Configuration must be a mapping.")
        configured = config.get("library_roots", [])

    if not isinstance(configured, list):
        raise ValueError("library_roots must be a list.")

    roots = []
    for entry in configured:
        if not isinstance(entry, dict) or not entry.get("path"):
            raise ValueError("Every library root must contain a path.")
        path = str(entry["path"])
        roots.append({
            "path": path,
            "category": str(entry.get("category") or ""),
            "reachable": os.path.isdir(path),
        })
    return roots


def load_db(db_path: str | Path) -> dict:
    """Read a consistent snapshot using a connection opened in read-only mode."""
    queries = {
        "books": """
            SELECT id, title, author, narrator, category, duration_seconds,
                   file_path, cue_path, has_cover, file_size, file_hash
            FROM books
        """,
        "chapters": "SELECT id, book_id, file_path FROM chapters",
        "albums": """
            SELECT id, title, artist_id, folder_path, has_cover, track_count
            FROM albums
        """,
        "artists": "SELECT id, name FROM artists",
        "tracks": """
            SELECT id, title, album_id, artist_id, file_path, file_size,
                   file_hash, duration_seconds, content_kind
            FROM tracks
        """,
    }
    # as_uri also protects filenames containing URI delimiters such as # or ?.
    path = Path(db_path).resolve().as_uri()
    connection = sqlite3.connect(f"{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN")
        return {
            table: [dict(row) for row in connection.execute(query)]
            for table, query in queries.items()
        }
    finally:
        connection.close()


def walk_roots(roots: list[dict]) -> dict:
    result = {
        "audio_files": [],
        "other_files": [],
        "empty_dirs": [],
        "errors": [],
    }
    seen_files = set()
    empty_dirs = set()

    def onerror(error: OSError) -> None:
        result["errors"].append({
            "path": str(error.filename or ""),
            "operation": "walk",
            "error": str(error),
        })

    for root in roots:
        reachable = root.get("reachable")
        if reachable is None:
            reachable = os.path.isdir(root["path"])
        if not reachable:
            continue

        for directory, subdirs, filenames in os.walk(
            root["path"], topdown=False, onerror=onerror, followlinks=False
        ):
            normalized_directory = normalize_path(directory)
            # An unvisited child, including an unreadable directory or directory
            # symlink, prevents an ancestor from being declared empty.
            if not filenames and all(
                normalize_path(os.path.join(directory, child)) in empty_dirs
                for child in subdirs
            ):
                if normalized_directory not in empty_dirs:
                    empty_dirs.add(normalized_directory)
                    result["empty_dirs"].append(str(directory))

            for filename in filenames:
                path = os.path.join(directory, filename)
                normalized = normalize_path(path)
                if normalized in seen_files:
                    continue
                seen_files.add(normalized)
                try:
                    size = os.stat(path).st_size
                except OSError as exc:
                    result["errors"].append({
                        "path": str(path),
                        "operation": "stat",
                        "error": str(exc),
                    })
                    continue
                bucket = (
                    "audio_files"
                    if Path(filename).suffix.lower() in AUDIO_EXTENSIONS
                    else "other_files"
                )
                result[bucket].append((str(path), size))

    return result


def _section(items: list[dict], count: int | None = None) -> dict:
    items = sorted(
        items,
        key=lambda item: (
            -item["bytes"],
            -_size(item.get("track_count")),
            str(item.get("title", item.get("folder", item.get("path", "")))),
        ),
    )
    return {
        "count": len(items) if count is None else count,
        "bytes": sum(item["bytes"] for item in items),
        "items": items,
    }


def check_duplicate_files(books: list[dict], tracks: list[dict]) -> dict:
    groups = defaultdict(list)
    for kind, rows in (("book", books), ("track", tracks)):
        for row in rows:
            if not row.get("file_path"):
                continue
            file_hash = row.get("file_hash")
            if file_hash is not None:
                key = ("exact", file_hash)
            else:
                key = (
                    "likely",
                    row.get("file_size"),
                    normalize_title(row.get("title")),
                )
            groups[key].append((kind, row))

    items = []
    for key, rows in groups.items():
        files_by_path = {}
        for kind, row in rows:
            normalized = normalize_path(row["file_path"])
            candidate = {
                "path": str(row["file_path"]),
                "size": _size(row.get("file_size")),
                "kind": kind,
                "id": row["id"],
                "title": row.get("title") or "",
            }
            previous = files_by_path.get(normalized)
            if previous is None or candidate["size"] > previous["size"]:
                files_by_path[normalized] = candidate

        if len(files_by_path) < 2:
            continue
        files = sorted(
            files_by_path.values(),
            key=lambda item: (-item["size"], normalize_path(item["path"])),
        )
        total = sum(item["size"] for item in files)
        wasted = total - max(item["size"] for item in files)
        items.append({
            "kind": key[0],
            "file_hash": key[1] if key[0] == "exact" else None,
            "title": files[0]["title"],
            "paths": [item["path"] for item in files],
            "files": files,
            "wasted_bytes": wasted,
            "bytes": wasted,
        })
    return _section(items)


def check_duplicate_books(
    books: list[dict], duplicate_files: dict | None = None
) -> dict:
    if duplicate_files is None:
        duplicate_files = check_duplicate_files(books, [])
    exact_path_sets = [
        {normalize_path(path) for path in item["paths"]}
        for item in duplicate_files["items"]
        if item["kind"] == "exact"
    ]

    groups = defaultdict(list)
    for book in books:
        groups[(
            normalize_title(book.get("title")),
            normalize_title(book.get("author")),
        )].append(book)

    items = []
    for rows in groups.values():
        if len(rows) < 2:
            continue
        paths = {normalize_path(row.get("file_path")) for row in rows}
        if "" not in paths and len(paths) > 1 and any(
            paths <= exact_paths for exact_paths in exact_path_sets
        ):
            continue
        entries = [
            {
                "id": row["id"],
                "path": row.get("file_path"),
                "size": _size(row.get("file_size")),
                "duration_seconds": row.get("duration_seconds"),
            }
            for row in rows
        ]
        entries.sort(key=lambda item: (-item["size"], str(item["id"])))
        items.append({
            "title": rows[0].get("title") or "",
            "author": rows[0].get("author") or "",
            "ids": [entry["id"] for entry in entries],
            "books": entries,
            "bytes": sum(entry["size"] for entry in entries),
        })
    return _section(items)


def check_missing_files(
    books: list[dict],
    tracks: list[dict],
    roots: list[dict],
    existing_paths: set[str],
) -> dict:
    """existing_paths is an externally collected snapshot; this check does no IO."""
    existing = {normalize_path(path) for path in existing_paths}
    items = []
    for kind, rows in (("book", books), ("track", tracks)):
        for row in rows:
            path = row.get("file_path")
            if _reachable_path(path, roots) and normalize_path(path) not in existing:
                items.append({
                    "kind": kind,
                    "id": row["id"],
                    "title": row.get("title") or "",
                    "path": path,
                    "bytes": _size(row.get("file_size")),
                })
    return _section(items)


def check_orphan_audio(
    audio_files: list[tuple[str, int]],
    books: list[dict],
    tracks: list[dict],
    chapters: list[dict],
) -> dict:
    known = {
        normalize_path(row.get("file_path"))
        for rows in (books, tracks, chapters)
        for row in rows
        if row.get("file_path")
    }
    folders = {}
    seen = set()
    for path, size in audio_files:
        normalized = normalize_path(path)
        if normalized in known or normalized in seen:
            continue
        seen.add(normalized)
        folder = str(Path(path.replace("\\", "/")).parent)
        key = normalize_path(folder)
        item = folders.setdefault(key, {
            "folder": folder, "file_count": 0, "bytes": 0,
        })
        item["file_count"] += 1
        item["bytes"] += _size(size)
    items = list(folders.values())
    return _section(items, sum(item["file_count"] for item in items))


def check_books_missing_metadata(books: list[dict], chapters: list[dict]) -> dict:
    chapter_counts = Counter(chapter["book_id"] for chapter in chapters)
    items = []
    for book in books:
        flags = []
        if not str(book.get("author") or "").strip():
            flags.append("no author")
        if not book.get("has_cover"):
            flags.append("no cover")
        if chapter_counts[book["id"]] == 0:
            flags.append("no chapters")
        if flags:
            items.append({
                "id": book["id"],
                "title": book.get("title") or "",
                "path": book.get("file_path"),
                "flags": flags,
                "bytes": 0,
            })
    return _section(items)


def check_albums_missing_cover(albums: list[dict]) -> dict:
    return _section([
        {
            "id": album["id"],
            "title": album.get("title") or "",
            "path": album.get("folder_path"),
            "track_count": _size(album.get("track_count")),
            "bytes": 0,
        }
        for album in albums
        if not album.get("has_cover")
    ])


def check_stray_files(other_files: list[tuple[str, int]]) -> dict:
    extensions = {}
    seen = set()
    for path, size in other_files:
        normalized = normalize_path(path)
        if normalized in seen:
            continue
        seen.add(normalized)
        filename = Path(path.replace("\\", "/")).name.lower()
        extension = Path(filename).suffix.lower()
        if (
            extension in AUDIO_EXTENSIONS
            or extension in SIDECAR_EXTENSIONS
            or filename in SIDECAR_NAMES
        ):
            continue
        item = extensions.setdefault(extension, {
            "extension": extension,
            "count": 0,
            "bytes": 0,
            "examples": [],
        })
        item["count"] += 1
        item["bytes"] += _size(size)
        item["examples"].append(str(path))

    items = list(extensions.values())
    for item in items:
        item["examples"] = sorted(item["examples"], key=normalize_path)[:3]
    return _section(items, sum(item["count"] for item in items))


def check_empty_folders(empty_dirs: list[str]) -> dict:
    unique = {}
    for path in empty_dirs:
        unique.setdefault(normalize_path(path), str(path))
    return _section([{"path": path, "bytes": 0} for path in unique.values()])


def _human_size(size: int) -> str:
    amount = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{amount:.1f} TB"


def _markdown(value: object) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return re.sub(r"([\\`*_{}\[\]<>|])", r"\\\1", text)


def _tail(path: str | None) -> str:
    if not path:
        return "(no path)"
    parts = [
        part for part in str(path).replace("\\", "/").split("/") if part
    ]
    return _markdown("/".join(parts[-2:]))


def _duration(value: object) -> str:
    try:
        seconds = max(0, int(float(value or 0)))
    except (ValueError, TypeError, OverflowError):
        seconds = 0
    hours, remainder = divmod(seconds, 3600)
    return f"{hours}h {remainder // 60}m"


def _render_item(key: str, item: dict) -> str:
    title = _markdown(item.get("title")) or "(untitled)"
    size = _human_size(item["bytes"])

    if key == "duplicate_files":
        details = "; ".join(
            f'{_tail(entry["path"])} ({_human_size(entry["size"])})'
            for entry in item["files"]
        )
        return f'{title} — {item["kind"]}, {size} extra; {details}'
    if key == "duplicate_books":
        author = _markdown(item["author"]) or "(no author)"
        details = "; ".join(
            f'ID {entry["id"]}, {_human_size(entry["size"])}, '
            f'{_duration(entry["duration_seconds"])}, {_tail(entry["path"])}'
            for entry in item["books"]
        )
        return f"{title} by {author} — {size}; {details}"
    if key == "missing_files":
        return (
            f'{title} — {item["kind"]} ID {item["id"]}, {size}; '
            f'{_tail(item["path"])}'
        )
    if key == "orphan_audio":
        return (
            f'{_tail(item["folder"])} — {item["file_count"]:,} audio files, {size}'
        )
    if key == "books_missing_metadata":
        return (
            f'{title} — ID {item["id"]}, {", ".join(item["flags"])}; '
            f'{_tail(item["path"])}'
        )
    if key == "albums_missing_cover":
        return (
            f'{title} — {item["track_count"]:,} tracks; {_tail(item["path"])}'
        )
    if key == "stray_files":
        extension = _markdown(item["extension"]) or "(no extension)"
        examples = "; ".join(_tail(path) for path in item["examples"])
        return f'{extension} — {item["count"]:,} files, {size}; {examples}'
    return _tail(item["path"])


def render_markdown(result: dict, top: int) -> str:
    sections = [
        (key, section)
        for key, section in result["sections"].items()
        if section["count"]
    ]
    sections.sort(key=lambda entry: (-entry[1]["bytes"], -entry[1]["count"]))

    lines = [
        "# Library tidy: what looks off",
        "",
        "Read-only check. Nothing has been moved or changed. "
        "Pick the lines you want fixed.",
        "",
        "| # | What looks off | How many | Size |",
        "| --- | --- | ---: | ---: |",
    ]
    for number, (key, section) in enumerate(sections, 1):
        lines.append(
            f'| {number} | {SECTION_NAMES[key]} | {section["count"]:,} | '
            f'{_human_size(section["bytes"])} |'
        )

    if not sections:
        lines.extend(["", "Nothing looks off in the files checked."])

    for number, (key, section) in enumerate(sections, 1):
        lines.extend(["", f"## {number}. {SECTION_NAMES[key]}", ""])
        for item in section["items"][:max(0, top)]:
            lines.append(f"- {_render_item(key, item)}")
        remaining = len(section["items"]) - max(0, top)
        if remaining > 0:
            lines.extend(["", f"{remaining:,} more entries in this section."])

    offline = [
        _markdown(root["path"])
        for root in result["roots"]
        if not root["reachable"]
    ]
    lines.extend([
        "",
        "Not checked: offline roots: "
        + ("; ".join(offline) if offline else "none")
        + f'; {result["not_checked"]:,} book/track rows '
        "(drive offline / outside roots).",
    ])
    if result["errors"]:
        lines.extend([
            "",
            f'Read errors: {len(result["errors"]):,}. Details are in the JSON report.',
        ])
    return "\n".join(lines) + "\n"


def _validate_outputs(outputs: list[Path], roots: list[dict]) -> None:
    """Reports never land inside a library root."""
    for output in outputs:
        key = normalize_path(output.resolve())
        if any(_under_path(key, normalize_path(root["path"])) for root in roots):
            raise ValueError(f"Report destination is inside a library root: {output}")


def _nonnegative_int(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return number


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=SERVER / "audiplex.db")
    parser.add_argument("--config", type=Path, default=SERVER / "config.yaml")
    parser.add_argument("--out-md", type=Path)
    parser.add_argument("--out-json", type=Path)
    parser.add_argument("--top", type=_nonnegative_int, default=15)
    args = parser.parse_args(argv)

    try:
        roots = load_roots(args.config)
        db = load_db(args.db)
        walked = walk_roots(roots)

        existing_paths = set()
        not_checked = 0
        checked_paths = set()
        for rows in (db["books"], db["tracks"]):
            for row in rows:
                path = row.get("file_path")
                if not _reachable_path(path, roots):
                    not_checked += 1
                    continue
                normalized = normalize_path(path)
                if normalized in checked_paths:
                    continue
                checked_paths.add(normalized)
                if os.path.exists(path):
                    existing_paths.add(normalized)

        duplicate_files = check_duplicate_files(db["books"], db["tracks"])
        sections = {
            "duplicate_files": duplicate_files,
            "duplicate_books": check_duplicate_books(db["books"], duplicate_files),
            "missing_files": check_missing_files(
                db["books"], db["tracks"], roots, existing_paths
            ),
            "orphan_audio": check_orphan_audio(
                walked["audio_files"], db["books"], db["tracks"], db["chapters"]
            ),
            "books_missing_metadata": check_books_missing_metadata(
                db["books"], db["chapters"]
            ),
            "albums_missing_cover": check_albums_missing_cover(db["albums"]),
            "stray_files": check_stray_files(walked["other_files"]),
            "empty_folders": check_empty_folders(walked["empty_dirs"]),
        }
        result = {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "roots": roots,
            "sections": sections,
            "not_checked": not_checked,
            "errors": walked["errors"],
        }
        markdown = render_markdown(result, args.top)
        outputs = [
            path for path in (args.out_md, args.out_json) if path is not None
        ]
        _validate_outputs(outputs, roots)

        if args.out_md is not None:
            args.out_md.write_text(markdown, encoding="utf-8")
        if args.out_json is not None:
            args.out_json.write_text(
                json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        if not outputs:
            print(markdown, end="")
        return 0
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"Library audit failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
