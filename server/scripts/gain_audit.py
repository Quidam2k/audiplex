"""Audit Android per-track loudness gain for #3504.
Detect stale previous-track gain after a ducked track change.
Read JSONL samples and loudness metadata from read-only SQLite.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import time
from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any

Row = dict[str, Any]
Lufs = Mapping[int, float | None]
ROOT = Path(__file__).resolve().parents[1]


def gain(lufs: float | None, target: float = -24.0) -> float:
    if lufs is None:
        return 1.0
    exponent = (target - lufs) / 20.0
    if exponent >= 0:
        return 1.0
    if exponent <= math.log10(0.05):
        return 0.05
    return 10.0 ** exponent


def _normalize(row: Any) -> Row | None:
    if not isinstance(row, dict):
        return None
    if "kind" in row and row["kind"] not in ("state", "volume"):
        return None
    if row.get("playing") is False:
        return None
    track = row.get("track_id")
    at, volume = row.get("at"), row.get("volume")
    if not isinstance(track, int) or isinstance(track, bool):
        return None
    for value in (at, volume):
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
        ):
            return None
    return {"at": float(at), "track_id": track, "volume": float(volume)}


def load_rows(
    paths: Iterable[str | Path], since_at: float | None = None
) -> list[Row]:
    rows: list[Row] = []
    for path in paths:
        with Path(path).open(encoding="utf-8") as source:
            for line in source:
                try:
                    row = _normalize(json.loads(line))
                except (ValueError, OverflowError):
                    continue
                if row is not None and (
                    since_at is None or row["at"] >= since_at
                ):
                    rows.append(row)
    return sorted(rows, key=lambda row: row["at"])


def load_lufs(
    db_path: str | Path, ids: Iterable[int]
) -> tuple[dict[int, float | None], dict[int, str]]:
    track_ids = sorted(set(ids))
    loudness: dict[int, float | None] = dict.fromkeys(track_ids)
    titles = dict.fromkeys(track_ids, "")
    if not track_ids:
        return loudness, titles

    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        for offset in range(0, len(track_ids), 900):
            chunk = track_ids[offset:offset + 900]
            placeholders = ",".join("?" for _ in chunk)
            query = (
                "SELECT id, loudness_lufs, title FROM tracks "
                f"WHERE id IN ({placeholders})"
            )
            for track_id, lufs, title in connection.execute(query, chunk):
                loudness[track_id] = None if lufs is None else float(lufs)
                titles[track_id] = title or ""
    finally:
        connection.close()
    return loudness, titles


def _segments(rows: Iterable[Row]) -> list[list[Row]]:
    normalized = []
    for row in rows:
        clean = _normalize(row)
        if clean is not None:
            normalized.append(clean)
    normalized.sort(key=lambda row: row["at"])

    segments: list[list[Row]] = []
    for row in normalized:
        if not segments or segments[-1][0]["track_id"] != row["track_id"]:
            segments.append([])
        segments[-1].append(row)
    return segments


def _slider(segments: list[list[Row]], lufs: Lufs, target: float) -> float:
    implied = [
        max(row["volume"] for row in segment)
        / gain(lufs.get(segment[0]["track_id"]), target)
        for segment in segments
        if len(segment) >= 2
    ]
    return min(1.0, median(implied)) if implied else 0.0


def estimate_slider(
    rows: Iterable[Row], lufs: Lufs, target: float = -24.0
) -> float:
    return _slider(_segments(rows), lufs, target)


def audit(rows: Iterable[Row], lufs: Lufs, target: float) -> list[Row]:
    segments = _segments(rows)
    slider = _slider(segments, lufs, target)
    results: list[Row] = []

    for index, segment in enumerate(segments):
        if len(segment) < 2:
            continue
        track_id = segment[0]["track_id"]
        steady = max(row["volume"] for row in segment)
        expected = min(1.0, slider * gain(lufs.get(track_id), target))
        prev_expected = None
        if index:  # the level the previous track actually played at, which a stale gain carries over
            prev_expected = max(row["volume"] for row in segments[index - 1])

        if abs(steady - expected) <= max(0.02, 0.1 * expected):
            verdict = "ok"
        elif (
            prev_expected is not None
            and abs(steady - prev_expected)
            <= max(0.02, 0.1 * prev_expected)
        ):
            verdict = "stale_prev_gain"
        else:
            verdict = "mismatch"

        # Silence has no finite logarithmic ratio; report it as unavailable.
        error_db = (
            round(20.0 * (math.log10(steady) - math.log10(expected)), 1)
            if steady > 0 and expected > 0
            else None
        )
        results.append({
            "track_id": track_id,
            "start_at": segment[0]["at"],
            "rows": len(segment),
            "steady": steady,
            "expected": expected,
            "prev_expected": prev_expected,
            "verdict": verdict,
            "started_ducked": segment[0]["volume"] < 0.5 * steady,
            "error_db": error_db,
        })
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "audiplex.db")
    parser.add_argument("--target", type=float, default=-24.0)
    parser.add_argument("--since-hours", type=float, default=24.0)
    parser.add_argument("files", nargs="*", type=Path)
    args = parser.parse_args(argv)

    paths = args.files or [
        path
        for path in (
            ROOT / "data" / "playback-diag.jsonl",
            ROOT / "data" / "gain_samples.jsonl",
        )
        if path.exists()
    ]
    rows = load_rows(paths, time.time() - args.since_hours * 3600.0)
    lufs, titles = load_lufs(args.db, (row["track_id"] for row in rows))
    results = audit(rows, lufs, args.target)

    for result in results:
        stamp = datetime.fromtimestamp(result["start_at"]).strftime("%H:%M:%S")
        error = result["error_db"]
        error_text = "n/a" if error is None else f"{error:+.1f}"
        title = titles.get(result["track_id"], "")[:40]
        print(
            f"{stamp} {result['verdict']} error_db={error_text} "
            f"started_ducked={result['started_ducked']} "
            f"{result['track_id']} {title}"
        )

    counts = Counter(result["verdict"] for result in results)
    ducked = [result for result in results if result["started_ducked"]]
    stale_ducked = sum(
        result["verdict"] == "stale_prev_gain" for result in ducked
    )
    print(
        f"ok={counts['ok']} stale_prev_gain={counts['stale_prev_gain']} "
        f"mismatch={counts['mismatch']} "
        f"slider={estimate_slider(rows, lufs, args.target):.4f}; "
        "stale_prev_gain among segments that started ducked: "
        f"{stale_ducked}/{len(ducked)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
