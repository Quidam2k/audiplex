#!/usr/bin/env python3
"""Read-only Audiplex loudness report for issues #3981, #3504, and #4222."""
import argparse
import math
import sqlite3
import sys
from collections import Counter
from pathlib import Path


def percentile(values, fraction):
    """Nearest-rank percentile; return None for an empty collection."""
    values = sorted(value for value in values if value is not None)
    return values[max(0, math.ceil(len(values) * fraction) - 1)] if values else None


def fmt(value):
    return "n/a" if value is None else f"{value:.1f}"


def cell(value):
    return str(value).replace("\\", "\\\\").replace("|", "\\|").replace(
        "\r", " "
    ).replace("\n", " ")


def table(headers, rows):
    return ["", "| " + " | ".join(map(cell, headers)) + " |",
            "| " + " | ".join("---" for _ in headers) + " |"] + [
        "| " + " | ".join(map(cell, row)) + " |" for row in rows
    ]


def query(conn, sql, params=()):
    return conn.execute(sql, params).fetchall()


def loudness(conn):
    return [row[0] for row in query(
        conn, "SELECT loudness_lufs FROM tracks WHERE loudness_lufs IS NOT NULL"
    )]


def coverage(conn):
    total, measured = query(
        conn, "SELECT COUNT(*), COUNT(loudness_lufs) FROM tracks"
    )[0]
    profiles = query(conn, "SELECT COUNT(*) FROM track_audio_profile")[0][0]
    dipped = query(conn, "SELECT COUNT(DISTINCT track_id) FROM track_dips")[0][0]
    dips, textured = query(conn, "SELECT COUNT(*), COUNT(texture) FROM track_dips")[0]
    percent = f"{100 * textured / dips:.1f}%" if dips else "n/a"
    return ["## 1. Coverage"] + table(["Count", "Metric"], [
        (total, "Tracks"), (measured, "Tracks with loudness"),
        (profiles, "Audio profiles"), (dipped, "Tracks with ≥1 dip"),
        (dips, "Dips"), (f"{textured} ({percent})", "Dips with texture"),
    ])


def music_distribution(conn):
    values = loudness(conn)
    lines = ["## 2. Music loudness distribution"]
    lines += table(["LUFS", "Statistic"], [
        (fmt(percentile(values, p)), label)
        for p, label in [(0, "Min"), (.05, "P5"), (.25, "P25"),
                         (.5, "Median"), (.75, "P75"), (.95, "P95"), (1, "Max")]
    ])
    bins = [0] * 18
    for value in values:
        bins[min(17, max(0, int((value + 40) // 2)))] += 1
    rows = []
    for index, count in enumerate(bins):
        low = -40 + index * 2
        label = f"[{low}, {low + 2})"
        if index == 0:
            label = "< -38 (includes values below -40)"
        elif index == 17:
            label = "≥ -6 (includes values above -4)"
        rows.append((count, label))
    return lines + table(["Tracks", "LUFS bin"], rows)


def attenuation(conn, target):
    all_values = loudness(conn)
    played_values = [row[0] for row in query(conn, """
        SELECT loudness_lufs FROM tracks t WHERE loudness_lufs IS NOT NULL
        AND EXISTS (SELECT 1 FROM play_stats p WHERE p.track_id = t.id)
    """)]
    lines = [f"## 3. Attenuate-only at {target:.1f} LUFS"]
    rows = []
    for scope, values in [("All", all_values), ("Played", played_values)]:
        cuts = [value - target for value in values if value > target]
        rows.extend([
            (scope, len(cuts), "Tracks cut"),
            (scope, fmt(percentile(cuts, .5)), "Median cut (dB)"),
            (scope, fmt(percentile(cuts, .95)), "P95 cut (dB)"),
            (scope, sum(cut > 6 for cut in cuts), "Cut >6 dB"),
            (scope, sum(v <= target for v in values), "No change: at/below target"),
            (scope, sum(v < target - 3 for v in values), "Boost candidates: >3 LU below"),
            (scope, sum(v < target - 6 for v in values), "Boost candidates: >6 LU below"),
        ])
    lines += table(["Scope", "Value", "Metric"], rows)
    tracks = query(conn, """
        SELECT t.title, COALESCE(a.name, ''), t.loudness_lufs, p.plays
        FROM tracks t LEFT JOIN artists a ON a.id = t.artist_id
        JOIN (SELECT track_id, COUNT(*) AS plays FROM play_stats
              GROUP BY track_id) p ON p.track_id = t.id
        WHERE t.loudness_lufs < ?
        ORDER BY p.plays DESC, t.loudness_lufs, t.id LIMIT 10
    """, (target - 3,))
    lines += ["", f"Most-played tracks below {target - 3:.1f} LUFS:"]
    return lines + table(["Plays", "LUFS", "Title", "Artist"], [
        (plays, fmt(value), title, artist) for title, artist, value, plays in tracks
    ])


def dips(conn):
    records = query(conn, "SELECT kind, end_s - start_s, depth_db FROM track_dips")
    rows = []
    for kind in ("intro", "mid", "outro"):
        selected = [row for row in records if row[0] == kind]
        durations = [row[1] for row in selected]
        depths = [row[2] for row in selected]
        rows.append((len(selected), kind, fmt(percentile(durations, .5)),
                     fmt(percentile(durations, .9)), fmt(percentile(depths, .5)),
                     fmt(percentile(depths, .9))))
    lines = ["## 4. Dips"] + table(
        ["Count", "Kind", "Median s", "P90 s", "Median depth dB", "P90 depth dB"], rows
    )
    intros = query(conn, """
        SELECT COUNT(*) FROM tracks t WHERE
        EXISTS (SELECT 1 FROM track_dips d WHERE d.track_id = t.id
                AND d.kind = 'intro' AND d.end_s - d.start_s >= 10)
        OR EXISTS (SELECT 1 FROM track_audio_profile p WHERE p.track_id = t.id
                   AND p.intro_quiet_s >= 10)
    """)[0][0]
    fades = [row[0] for row in query(
        conn, "SELECT outro_fade_s FROM track_audio_profile WHERE outro_fade_s IS NOT NULL"
    )]
    long_fades = [value for value in fades if value >= 10]
    lines += table(["Value", "Metric"], [
        (intros, "Tracks with intro dip or intro quiet ≥10 s"),
        (len(long_fades), "Outro fades ≥10 s"),
        (fmt(percentile(fades, .5)), "Median outro fade (s; all measured)"),
        (fmt(percentile(long_fades, .5)), "Median outro fade (s; ≥10 s)"),
    ])
    longest = query(conn, """
        SELECT p.intro_quiet_s, t.title, COALESCE(a.name, '')
        FROM track_audio_profile p JOIN tracks t ON t.id = p.track_id
        LEFT JOIN artists a ON a.id = t.artist_id
        WHERE p.intro_quiet_s IS NOT NULL
        ORDER BY p.intro_quiet_s DESC, t.id LIMIT 10
    """)
    lines += ["", "Longest quiet intros:"]
    lines += table(["Seconds", "Title", "Artist"], [
        (fmt(seconds), title, artist) for seconds, title, artist in longest
    ])
    textures = Counter(row[0] for row in query(conn, "SELECT texture FROM track_dips"))
    return lines + table(["Dips", "Texture"], [
        (count, "unlabeled" if texture is None else texture)
        for texture, count in sorted(textures.items(), key=lambda item: str(item[0]))
    ])


def spot_check(conn):
    matches = query(conn, """
        SELECT t.id, t.title, COALESCE(a.name, ''), t.loudness_lufs,
               p.intro_quiet_s, p.outro_fade_s
        FROM tracks t LEFT JOIN artists a ON a.id = t.artist_id
        LEFT JOIN track_audio_profile p ON p.track_id = t.id
        WHERE t.title LIKE '%Get Up, Stand Up%' OR t.title LIKE '%Get Up Stand Up%'
        ORDER BY t.id
    """)
    lines = ["## 5. Spot check"]
    if not matches:
        return lines + ["", "No matching tracks."]
    for track_id, title, artist, value, intro, outro in matches:
        lines += ["", f"Track {track_id}: {cell(title)} — {cell(artist)}"]
        lines += table(["LUFS", "Intro quiet s", "Outro fade s"],
                       [(fmt(value), fmt(intro), fmt(outro))])
        rows = query(conn, """
            SELECT start_s, end_s, depth_db, kind, texture FROM track_dips
            WHERE track_id = ? ORDER BY start_s, id
        """, (track_id,))
        lines += table(["Start s", "End s", "Depth dB", "Kind", "Texture"], [
            (fmt(start), fmt(end), fmt(depth), kind,
             "unlabeled" if texture is None else texture)
            for start, end, depth, kind, texture in rows
        ])
        if not rows:
            lines += ["", "No dips."]
    return lines


def books(conn, target, max_boost):
    lines = [f"## 6. Books at {target:.1f} LUFS; boost cap {max_boost:.1f} dB"]
    if "loudness_lufs" not in {row[1] for row in query(conn, "PRAGMA table_info(books)")}:
        return lines + ["", "books not measured yet"]
    categories = query(conn, """
        SELECT COUNT(loudness_lufs), COUNT(*), category FROM books
        GROUP BY category ORDER BY category
    """)
    lines += table(["Measured", "Total", "Category"], [
        (measured, total, category if category is not None else "uncategorized")
        for measured, total, category in categories
    ])
    values = [row[0] for row in query(
        conn, "SELECT loudness_lufs FROM books WHERE loudness_lufs IS NOT NULL"
    )]
    raises = [max(0.0, target - value) for value in values]
    rows = [(fmt(percentile(values, p)), f"{label} loudness (LUFS)")
            for p, label in [(0, "Min"), (.5, "Median"), (1, "Max")]]
    rows += [(fmt(percentile(raises, p)), f"{label} raise needed (dB)")
             for p, label in [(.5, "Median"), (.9, "P90"), (1, "Max")]]
    rows += [(sum(r > 6 for r in raises), "Need >6 dB raise"),
             (sum(r > max_boost for r in raises), "Raise exceeds boost cap"),
             (sum(v > target for v in values), "Louder than target; would be cut")]
    lines += table(["Value", "Metric"], rows)
    quietest = query(conn, """
        SELECT title, author, loudness_lufs FROM books WHERE loudness_lufs IS NOT NULL
        ORDER BY loudness_lufs, id LIMIT 10
    """)
    return lines + table(["LUFS", "Clamped raise dB", "Title", "Author"], [
        (fmt(value), fmt(min(max_boost, max(0.0, target - value))), title, author or "")
        for title, author, value in quietest
    ])


def main():
    sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path(__file__).resolve().parents[1] / "audiplex.db")
    parser.add_argument("--target", type=float, default=-20.0)
    parser.add_argument("--book-target", type=float, default=-20.0)
    parser.add_argument("--book-max-boost", type=float, default=12.0)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if not all(math.isfinite(v) for v in (args.target, args.book_target, args.book_max_boost)):
        parser.error("targets and boost cap must be finite")
    if args.book_max_boost < 0:
        parser.error("--book-max-boost must be nonnegative")
    db = args.db.resolve()
    if args.out and args.out.resolve() == db:
        parser.error("--out must differ from --db")
    conn = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)
    try:
        conn.execute("BEGIN")
        sections = [coverage(conn), music_distribution(conn), attenuation(conn, args.target),
                    dips(conn), spot_check(conn), books(conn, args.book_target, args.book_max_boost)]
        report = "# Audiplex loudness report\n\n" + "\n\n".join("\n".join(s) for s in sections) + "\n"
    finally:
        conn.close()
    if args.out:
        args.out.write_text(report, encoding="utf-8")
    sys.stdout.write(report)


if __name__ == "__main__":
    main()

