"""#3255/#7109: join every manual volume change on a ride to what was playing, its loudness, speech/duck state, GPS position and speed; summarize causes and draw a heat map. Read-only on both databases."""

from __future__ import annotations

import argparse
import bisect
import csv
import json
import math
import sqlite3
import statistics
import sys
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


BANDS = ("stopped", "slow", "cruise", "fast", "very fast")
BAND_LABELS = {
    "stopped": "<1 m/s",
    "slow": "1–<3 m/s",
    "cruise": "3–<6 m/s",
    "fast": "6–<9 m/s",
    "very fast": ">=9 m/s",
}
EXTRA_FIELDS = (
    "stream", "dir", "before", "after", "max", "source", "route",
    "music_active", "mic_dbfs", "mic_age_ms", "speed_mps", "speed_age_ms",
    "tts_playing", "ducked", "bike_mode", "biking_ctx", "ride_auto_vol",
)
CSV_FIELDS = (
    "timestamp", "epoch_seconds", "device_class", *EXTRA_FIELDS,
    "delta", "step_db", "lat", "lon", "gps_speed", "gps_ts",
    "gps_distance_seconds", "analysis_speed_mps", "ride_id", "on_ride",
    "interval_ride_id", "ride_start_ts", "ride_end_ts", "ride_distance_m",
    "track_id", "title", "artist", "lufs", "seconds_into_track",
    "mic_fresh", "duck_transition", "stop_transition", "extras_json",
)


class AnalysisRows(list):
    """Rows plus library information that cannot be inferred from changes."""

    def __init__(self, rows=()):
        super().__init__(rows)
        self.library_lufs = {}
        self.played_track_ids = set()
        self.owner_found = True
        self.owner = ""
        self.since = ""


def number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def integer(value):
    result = number(value)
    return int(result) if result is not None and result.is_integer() else None


def flag(value):
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    return bool(value)


def epoch(value):
    """Interpret naive dates/times as UTC, including Audiplex timestamps."""
    parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).timestamp()


def iso_time(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def open_db(path):
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def fresh(age, limit):
    age = number(age)
    return age is not None and 0 <= age <= limit


def valid_position(row):
    lat, lon = number(row.get("lat")), number(row.get("lon"))
    return (
        lat is not None and lon is not None
        and -90 <= lat <= 90 and -180 <= lon <= 180
    )


def load_changes(connection, since, include_app=False):
    since_epoch = epoch(since)
    # Use the UTC day as the SQL lower bound, then enforce the exact instant.
    # This also handles ISO strings with differing fractional-second precision.
    lower_bound = datetime.fromtimestamp(
        since_epoch, timezone.utc
    ).date().isoformat()
    records = connection.execute(
        "SELECT timestamp, extras, device_class FROM device_logs "
        "WHERE tag='vol_change' AND timestamp >= ? ORDER BY timestamp",
        (lower_bound,),
    )
    rows = AnalysisRows()
    for record in records:
        if record["device_class"] == "emulator":
            continue
        try:
            extras = json.loads(record["extras"] or "{}")
            timestamp = epoch(record["timestamp"])
        except (TypeError, ValueError, OverflowError):
            continue
        if not isinstance(extras, dict) or timestamp < since_epoch:
            continue
        if extras.get("source") != "user" and not include_app:
            continue
        if extras.get("dir") not in ("up", "down"):
            continue
        before, after = integer(extras.get("before")), integer(extras.get("after"))
        if before is None or after is None:
            continue
        row = {key: extras.get(key) for key in EXTRA_FIELDS}
        row.update(
            timestamp=record["timestamp"],
            epoch_seconds=timestamp,
            device_class=record["device_class"],
            before=before,
            after=after,
            max=integer(extras.get("max")),
            delta=after - before,
            step_db=after - before,
            extras_json=json.dumps(extras, ensure_ascii=False, sort_keys=True),
        )
        for key in ("tts_playing", "ducked"):
            row[key] = flag(row[key])
        for key in ("mic_dbfs", "mic_age_ms", "speed_mps", "speed_age_ms"):
            row[key] = number(row[key])
        rows.append(row)
    rows.sort(key=lambda row: row["epoch_seconds"])
    return rows


def timestamp_divisor(values):
    values = [value for value in values if value is not None]
    return 1000.0 if values and max(values) > 1e11 else 1.0


def load_gps(connection):
    raw_points = list(connection.execute(
        "SELECT ts, lat, lon, speed_mps, ride_id FROM location_track"
    ))
    divisor = timestamp_divisor([number(row["ts"]) for row in raw_points])
    points = []
    for row in raw_points:
        timestamp = number(row["ts"])
        if timestamp is None:
            continue
        speed = number(row["speed_mps"])
        points.append({
            "ts": timestamp / divisor,
            "lat": number(row["lat"]),
            "lon": number(row["lon"]),
            "speed_mps": speed if speed is not None and speed >= 0 else None,
            "ride_id": row["ride_id"],
        })
    points.sort(key=lambda point: point["ts"])

    raw_rides = list(connection.execute(
        "SELECT id, start_ts, end_ts, distance_m FROM location_rides"
    ))
    divisor = timestamp_divisor([
        number(row[key]) for row in raw_rides for key in ("start_ts", "end_ts")
    ])
    rides = []
    for row in raw_rides:
        start, end = number(row["start_ts"]), number(row["end_ts"])
        if start is None:
            continue
        start /= divisor
        end = end / divisor if end is not None else None
        if end is not None and end < start:
            continue
        rides.append({
            "id": row["id"],
            "start_ts": start,
            "end_ts": end,
            "distance_m": number(row["distance_m"]),
        })
    rides.sort(key=lambda ride: ride["start_ts"])
    return points, rides


def nearest_point(points, timestamp, timestamps=None, tolerance=15.0):
    if not points:
        return None
    if timestamps is None:
        timestamps = [point["ts"] for point in points]
    index = bisect.bisect_left(timestamps, timestamp)
    candidates = points[max(0, index - 1):min(len(points), index + 1)]
    point = min(candidates, key=lambda item: abs(item["ts"] - timestamp))
    return point if abs(point["ts"] - timestamp) <= tolerance else None


def index_rides(rides):
    starts, prefix_ends = [], []
    latest_end = -math.inf
    for ride in rides:
        starts.append(ride["start_ts"])
        latest_end = max(
            latest_end,
            ride["end_ts"] if ride["end_ts"] is not None else math.inf,
        )
        prefix_ends.append(latest_end)
    return rides, starts, prefix_ends


def ride_at(index, timestamp):
    rides, starts, prefix_ends = index
    position = bisect.bisect_right(starts, timestamp) - 1
    while position >= 0:
        if prefix_ends[position] < timestamp:
            break
        ride = rides[position]
        if ride["end_ts"] is None or timestamp <= ride["end_ts"]:
            return ride
        position -= 1
    return None


def load_tracks(connection, owner, since):
    owner_found = connection.execute(
        "SELECT 1 FROM users WHERE username = ? LIMIT 1", (owner,)
    ).fetchone() is not None
    sql = (
        "SELECT ps.track_id, ps.event, ps.timestamp, ps.user_id, "
        "t.title, t.duration_seconds, t.loudness_lufs, a.name AS artist "
        "FROM play_stats AS ps "
        "LEFT JOIN users AS u ON u.id = ps.user_id "
        "LEFT JOIN tracks AS t ON t.id = ps.track_id "
        "LEFT JOIN artists AS a ON a.id = t.artist_id"
    )
    parameters = ()
    if owner_found:
        sql += " WHERE u.username = ?"
        parameters = (owner,)

    starts = []
    endings = defaultdict(list)
    library_lufs = {}
    played_track_ids = set()
    since_epoch = epoch(since)

    for record in connection.execute(sql, parameters):
        try:
            timestamp = epoch(record["timestamp"])
        except (TypeError, ValueError, OverflowError):
            continue
        track_id = record["track_id"]
        if record["event"] != "start":
            endings[track_id].append(timestamp)
            continue
        loudness = number(record["loudness_lufs"])
        if timestamp >= since_epoch:
            played_track_ids.add(track_id)
            if loudness is not None:
                library_lufs[track_id] = loudness
        duration = number(record["duration_seconds"])
        if duration is None or duration < 0:
            continue
        starts.append({
            "start": timestamp,
            "track_id": track_id,
            "title": record["title"],
            "artist": record["artist"],
            "lufs": loudness,
            "duration_seconds": duration,
        })

    for times in endings.values():
        times.sort()
    starts.sort(key=lambda start: start["start"])
    prefix_ends = []
    latest_end = -math.inf
    for start in starts:
        valid_until = start["start"] + start["duration_seconds"] + 30
        stop_times = endings.get(start["track_id"], ())
        stop_index = bisect.bisect_left(stop_times, start["start"])
        if stop_index < len(stop_times):
            valid_until = min(valid_until, stop_times[stop_index])
        start["valid_until"] = valid_until
        latest_end = max(latest_end, valid_until)
        prefix_ends.append(latest_end)
    timeline = {
        "starts": starts,
        "times": [start["start"] for start in starts],
        "prefix_ends": prefix_ends,
    }
    return timeline, library_lufs, played_track_ids, owner_found


def load_diag_timeline(diag_path, connection):
    """Track changes the phone reported to the playback bus (data/playback-diag.jsonl).

    play_stats only has a few starts per day; the bus logs every track change
    with its position, so it is the better "what was playing" source.
    """
    states = []
    try:
        with open(diag_path, encoding="utf-8") as handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if entry.get("kind") == "state" and number(entry.get("at")) is not None:
                    states.append(entry)
    except OSError:
        return None
    states.sort(key=lambda entry: entry["at"])
    meta = {}
    ids = {entry.get("track_id") for entry in states if isinstance(entry.get("track_id"), int)}
    for record in connection.execute(
        "SELECT t.id, t.title, t.loudness_lufs, a.name AS artist FROM tracks AS t "
        "LEFT JOIN artists AS a ON a.id = t.artist_id"
    ):
        if record["id"] in ids:
            meta[record["id"]] = record
    starts = []
    for entry, following in zip(states, states[1:] + [None]):
        track_id = entry.get("track_id")
        if not entry.get("playing") or not isinstance(track_id, int) or track_id <= 0:
            continue
        position = (number(entry.get("position_ms")) or 0) / 1000
        duration = (number(entry.get("duration_ms")) or 0) / 1000
        start = entry["at"] - position
        valid_until = entry["at"] + max(duration - position, 0) + 5
        if following is not None:
            valid_until = min(valid_until, following["at"])
        record = meta.get(track_id)
        starts.append({
            "start": start,
            "track_id": track_id,
            "title": record["title"] if record else None,
            "artist": record["artist"] if record else None,
            "lufs": number(record["loudness_lufs"]) if record else None,
            "duration_seconds": duration,
            "valid_until": valid_until,
        })
    starts.sort(key=lambda start: start["start"])
    prefix_ends, latest_end = [], -math.inf
    for start in starts:
        latest_end = max(latest_end, start["valid_until"])
        prefix_ends.append(latest_end)
    return {"starts": starts, "times": [s["start"] for s in starts], "prefix_ends": prefix_ends}


def track_at(timeline, timestamp):
    """Return the latest start still valid under duration and stop-event rules."""
    position = bisect.bisect_right(timeline["times"], timestamp) - 1
    while position >= 0:
        if timeline["prefix_ends"][position] <= timestamp:
            break
        start = timeline["starts"][position]
        if timestamp < start["valid_until"]:
            return {
                "track_id": start["track_id"],
                "title": start["title"],
                "artist": start["artist"],
                "lufs": start["lufs"],
                "seconds_into_track": timestamp - start["start"],
            }
        position -= 1
    return None


def speed_band(speed):
    speed = number(speed)
    if speed is None or speed < 0:
        return None
    if speed < 1:
        return "stopped"
    if speed < 3:
        return "slow"
    if speed < 6:
        return "cruise"
    if speed < 9:
        return "fast"
    return "very fast"


def stop_windows(points, ride_index):
    """Detect >3 to <1 m/s drops within 60 seconds, separately for each ride."""
    grouped = defaultdict(list)
    for point in points:
        ride_id = point["ride_id"]
        if ride_id is None:
            ride = ride_at(ride_index, point["ts"])
            ride_id = ride["id"] if ride else day_key(point["ts"])  # unclosed ride: group by day
        grouped[ride_id].append(point)

    windows = defaultdict(list)
    for ride_id, ride_points in grouped.items():
        last_fast = None
        for point in ride_points:
            speed = point["speed_mps"]
            if speed is None:
                continue
            if speed > 3:
                last_fast = point["ts"]
            elif speed < 1:
                if last_fast is not None and point["ts"] - last_fast <= 60:
                    windows[ride_id].append(point["ts"])
                last_fast = None
    return windows


def day_key(timestamp):
    return "day:" + datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d")


def in_stop_window(windows, ride_id, timestamp):
    times = windows.get(ride_id, ())
    index = bisect.bisect_right(times, timestamp) - 1
    return index >= 0 and timestamp - times[index] <= 30


def join_changes(rows, points, rides, timeline, diag_timeline=None):
    timestamps = [point["ts"] for point in points]
    ride_index = index_rides(rides)
    windows = stop_windows(points, ride_index)
    previous = None
    for row in rows:
        timestamp = row["epoch_seconds"]
        point = nearest_point(points, timestamp, timestamps)
        ride = ride_at(ride_index, timestamp)
        speed = row["speed_mps"]
        if speed is None or speed < 0 or not fresh(row["speed_age_ms"], 15000):
            speed = point["speed_mps"] if point else None
        row.update(
            lat=point["lat"] if point else None,
            lon=point["lon"] if point else None,
            gps_speed=point["speed_mps"] if point else None,
            gps_ts=point["ts"] if point else None,
            gps_distance_seconds=abs(point["ts"] - timestamp) if point else None,
            analysis_speed_mps=speed,
            ride_id=point["ride_id"] if point else None,
            on_ride=ride is not None,
            interval_ride_id=ride["id"] if ride else None,
            ride_start_ts=ride["start_ts"] if ride else None,
            ride_end_ts=ride["end_ts"] if ride else None,
            ride_distance_m=ride["distance_m"] if ride else None,
            track_id=None,
            title=None,
            artist=None,
            lufs=None,
            seconds_into_track=None,
            mic_fresh=(
                row["mic_dbfs"] is not None and fresh(row["mic_age_ms"], 5000)
            ),
            stop_transition=in_stop_window(
                windows, ride["id"] if ride else day_key(timestamp), timestamp
            ),
            duck_transition=bool(
                previous
                and timestamp - previous["epoch_seconds"] <= 10
                and (previous["tts_playing"] or previous["ducked"])
                and not row["tts_playing"]
                and not row["ducked"]
            ),
        )
        if row["ride_id"] is None and ride:
            row["ride_id"] = ride["id"]
        if not row["on_ride"] and row.get("biking_ctx"):  # a ride still in progress has no ride row yet
            row["on_ride"] = True
        track = (track_at(diag_timeline, timestamp) if diag_timeline else None) or track_at(timeline, timestamp)
        if track:
            row.update(track)
        previous = row


def collapse_adjustments(rows):
    adjustments = []
    last_timestamp = None
    for row in rows:
        timestamp = row["epoch_seconds"]
        if last_timestamp is None or timestamp - last_timestamp > 4:
            adjustment = dict(row)
            adjustment["change_count"] = 1
            adjustments.append(adjustment)
        else:
            adjustment = adjustments[-1]
            adjustment["delta"] += row["delta"]
            adjustment["step_db"] = adjustment["delta"]
            adjustment["after"] = row["after"]
            adjustment["max"] = row["max"]
            adjustment["change_count"] += 1
        adjustment = adjustments[-1]
        adjustment["end_timestamp"] = row["timestamp"]
        adjustment["dir"] = (
            "up" if adjustment["delta"] > 0
            else "down" if adjustment["delta"] < 0
            else "same"
        )
        last_timestamp = timestamp
    return adjustments


def percentile(values, fraction):
    values = sorted(values)
    if not values:
        return None
    position = (len(values) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def fmt(value, digits=2):
    return "unavailable" if value is None else f"{value:.{digits}f}"


def mean(values):
    values = [value for value in values if value is not None]
    return statistics.mean(values) if values else None


def distribution(values):
    values = [value for value in values if value is not None]
    if not values:
        return "unavailable (0 observations)"
    q1, q3 = percentile(values, 0.25), percentile(values, 0.75)
    return (
        f"median {fmt(statistics.median(values))}; "
        f"middle half {fmt(q1)}–{fmt(q3)}; IQR {fmt(q3 - q1)} "
        f"({len(values)} observations)"
    )


def directions(rows):
    ups = sum(row["dir"] == "up" for row in rows)
    downs = sum(row["dir"] == "down" for row in rows)
    return ups, downs, ups - downs


def md(value):
    return str(value if value is not None else "unknown").replace(
        "|", r"\|"
    ).replace("\r", " ").replace("\n", " ")


def table(headers, records):
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    lines.extend("| " + " | ".join(md(cell) for cell in row) + " |"
                 for row in records)
    return lines


def context_summary(rows, label):
    ups, downs, _ = directions(rows)
    on_ride = sum(bool(row["on_ride"]) for row in rows)
    known_track = sum(row["track_id"] is not None for row in rows)
    known_lufs = sum(row["lufs"] is not None for row in rows)
    same = len(rows) - ups - downs
    lines = [
        f"## {label}",
        "",
        f"{len(rows)} total: {ups} up, {downs} down"
        + (f", {same} net-zero" if same else "") + ".",
        f"{on_ride} on rides; {len(rows) - on_ride} off rides. "
        f"{known_track} have a known track; {known_lufs} have known loudness.",
        "",
        "Mean track loudness when pressing up: "
        f"{fmt(mean([row['lufs'] for row in rows if row['dir'] == 'up']))} LUFS. "
        "When pressing down: "
        f"{fmt(mean([row['lufs'] for row in rows if row['dir'] == 'down']))} LUFS.",
        "A more negative LUFS value means a quieter track.",
        "",
    ]
    grouped = defaultdict(list)
    for row in rows:
        if row["track_id"] is not None:
            grouped[row["track_id"]].append(row)
    records = []
    for track_rows in grouped.values():
        first = track_rows[0]
        up, down, net = directions(track_rows)
        records.append((
            first["title"], first["artist"], fmt(first["lufs"]), up, down, net
        ))
    records.sort(key=lambda item: (-item[-1], str(item[0]), str(item[1])))
    lines += table(("Title", "Artist", "LUFS", "Ups", "Downs", "Net"), records)
    if not records:
        lines.append("No tracks were matched.")
    lines += [
        "",
        "Speech and ducking:",
        "",
    ]
    records = []
    for field in ("tts_playing", "ducked"):
        for state in (True, False):
            selected = [row for row in rows if row[field] == state]
            records.append((f"{field}={str(state).lower()}", *directions(selected)))
    lines += table(("State", "Ups", "Downs", "Net"), records)
    duck_rows = [row for row in rows if row["duck_transition"]]
    up, down, _ = directions(duck_rows)
    lines += [
        "",
        f"Duck transitions: {len(duck_rows)} total, {up} up and {down} down.",
        "",
        "Speed:",
        "",
    ]
    records = []
    for band in BANDS:
        selected = [
            row for row in rows if speed_band(row["analysis_speed_mps"]) == band
        ]
        records.append((f"{band} ({BAND_LABELS[band]})", *directions(selected)))
    lines += table(("Band", "Ups", "Downs", "Net"), records)
    unknown = sum(speed_band(row["analysis_speed_mps"]) is None for row in rows)
    lines += [
        "",
        "Mean speed for ups: "
        f"{fmt(mean([row['analysis_speed_mps'] for row in rows if row['dir'] == 'up']))} "
        "m/s; for downs: "
        f"{fmt(mean([row['analysis_speed_mps'] for row in rows if row['dir'] == 'down']))} "
        f"m/s. {unknown} have no usable speed.",
    ]
    stop_rows = [row for row in rows if row["stop_transition"]]
    up, down, _ = directions(stop_rows)
    lines.append(f"Within 30 seconds after a detected stop: {down} down, {up} up.")
    for direction in ("up", "down"):
        selected = [row for row in rows if row["dir"] == direction]
        usable = [row["mic_dbfs"] for row in selected if row["mic_fresh"]]
        if usable:
            text = (
                f"mean {fmt(statistics.mean(usable))} dBFS "
                f"from {len(usable)} fresh readings"
            )
        else:
            text = "mic stale (no usable fresh readings)"
        lines.append(
            f"Mic for {direction}s: {text}; "
            f"{len(selected) - len(usable)} stale or missing."
        )
    lines.append("")
    return lines


def volume_summary(rows):
    lines = ["## Volume level on rides", ""]
    for name, predicate in (
        ("Moving (>=3 m/s)", lambda speed: speed >= 3),
        ("Stopped (<1 m/s)", lambda speed: speed < 1),
    ):
        selected = [
            row for row in rows
            if row["on_ride"] and row["analysis_speed_mps"] is not None
            and predicate(row["analysis_speed_mps"])
        ]
        maximums = sorted({
            row["max"] for row in selected
            if row["max"] is not None and row["max"] > 0
        })
        fractions = [
            row["after"] / row["max"] for row in selected
            if row["max"] is not None and row["max"] > 0
        ]
        lines += [
            f"{name}: after in device steps: "
            f"{distribution([row['after'] for row in selected])}.",
            f"Device maximums observed: {', '.join(map(str, maximums)) or 'unknown'}. "
            f"After/max: {distribution(fractions)}.",
            "",
        ]
    return lines


def speed_curve(adjustments):
    levels = defaultdict(list)
    for row in adjustments:
        band = speed_band(row["analysis_speed_mps"])
        maximum = row["max"]
        if (
            row["on_ride"] and band is not None and maximum is not None
            and maximum > 0 and 0 <= row["after"] <= maximum
        ):
            levels[band].append(row["after"] / maximum)
    medians = {
        band: statistics.median(values)
        for band, values in levels.items() if len(values) >= 3
    }
    cruise = medians.get("cruise")
    records = []
    for band in BANDS:
        count = len(levels[band])
        level = medians.get(band)
        if count < 3:
            records.append((band, count, "insufficient", "insufficient"))
            continue
        if cruise is None:
            offset = "insufficient cruise points"
        elif cruise <= 0 or level <= 0:
            offset = "undefined at zero level"
        else:
            offset = f"{20 * math.log10(level / cruise):+.2f} dB"
        records.append((band, count, f"{level:.3f}", offset))
    return [
        "## Speed curve fit",
        "",
        "This fit uses the median after/max level of adjustments on rides in "
        "each speed band. At least three points are required per band.",
        "Offsets use 20*log10(level/cruise_level) as a rough proxy. "
        "Android volume steps are not exactly linear amplitude.",
        "",
        *table(("Band", "Points", "Median after/max", "Offset from cruise"), records),
        "",
    ]


def cluster_summary(adjustments):
    cells = defaultdict(list)
    for row in adjustments:
        if row["on_ride"] and valid_position(row):
            cells[(round(row["lat"], 3), round(row["lon"], 3))].append(row)
    records = []
    for (lat, lon), members in cells.items():
        ride_ids = {
            row.get("interval_ride_id", row["ride_id"]) for row in members
        } - {None}
        if len(ride_ids) < 2:
            continue
        steps = sum(row["delta"] for row in members)
        direction = "up" if steps > 0 else "down" if steps < 0 else "balanced"
        up, down, net = directions(members)
        records.append((
            f"{lat:.3f}, {lon:.3f}", len(members), len(ride_ids),
            up, down, net, f"{direction} ({steps:+d} steps)",
        ))
    records.sort(key=lambda item: (-item[2], -item[1], item[0]))
    lines = [
        "## Repeated location clusters",
        "",
        "Coordinates are rounded to three decimals, roughly 100 m cells; "
        "longitude cell width varies with latitude. Only cells visited on "
        "at least two different rides are listed.",
        "",
        *table(
            ("Cell", "Adjustments", "Rides", "Ups", "Downs", "Net", "Net steps"),
            records,
        ),
    ]
    if not records:
        lines.append("No qualifying cells.")
    lines.append("")
    return lines


def summarize(rows) -> str:
    adjustments = collapse_adjustments(rows)
    lines = [
        "# Volume analysis",
        "",
        f"Window starts at {getattr(rows, 'since', '') or 'the supplied cutoff'} "
        "(UTC); ends at the available database records.",
        f"Owner: {md(getattr(rows, 'owner', '') or 'unspecified')}.",
    ]
    if not getattr(rows, "owner_found", True):
        lines.append("The owner was not found, so playback from all users was used.")
    lines += [
        "Both databases were opened read-only.",
        "",
        "These are associations around volume presses, not proof of their cause. "
        "Net in tally tables means ups minus downs. step_db is the signed "
        "after-before device-step difference, not a measured dB change.",
        "",
        *context_summary(rows, "Individual changes"),
        "## Played-library loudness",
        "",
    ]
    library = getattr(rows, "library_lufs", None)
    if library is None:
        library = {
            row["track_id"]: row["lufs"] for row in rows
            if row["track_id"] is not None and row["lufs"] is not None
        }
    values = list(library.values())
    played = getattr(rows, "played_track_ids", set(library))
    if values:
        q1, q3 = percentile(values, 0.25), percentile(values, 0.75)
        lines.append(
            f"Among {len(played)} distinct tracks started in the window, "
            f"{len(values)} have loudness values. Their LUFS minimum is "
            f"{fmt(min(values))}, median {fmt(statistics.median(values))}, "
            f"maximum {fmt(max(values))}; middle half {fmt(q1)}–{fmt(q3)}, "
            f"IQR {fmt(q3 - q1)} LUFS. Each track is counted once."
        )
    else:
        lines.append(
            f"{len(played)} distinct tracks were started in the window; "
            "their loudness spread is unavailable."
        )
    lines += [
        "",
        "## Adjustment bursts",
        "",
        "Consecutive changes no more than four seconds apart form one adjustment. "
        "Its delta is the sum of signed steps, its context comes from the first "
        "change, and its after/max comes from the final change. "
        "A net-zero adjustment remains in the total.",
        f"{len(adjustments)} adjustments; mean size "
        f"{fmt(mean([abs(row['delta']) for row in adjustments]))} absolute net "
        f"steps; mean signed delta "
        f"{fmt(mean([row['delta'] for row in adjustments]))} steps.",
        "",
        *context_summary(adjustments, "Tallies per adjustment"),
        "Duck-transition flags approximate a return from speech or ducking: "
        "the previous included change had either flag on, the current one has "
        "both off, and the gap is at most ten seconds. Adjustment transition "
        "tallies inherit the first change's flag.",
        "",
        "Stop windows begin at the first GPS point below 1 m/s following a "
        "point above 3 m/s within 60 seconds on the same ride. Each observation "
        "is counted at most once even if windows overlap.",
        "",
        *volume_summary(rows),
        *speed_curve(adjustments),
        *cluster_summary(adjustments),
        "## Yellow Brick Road",
        "",
    ]
    yellow = [
        row for row in rows
        if "yellow brick road" in str(row["title"] or "").casefold()
    ]
    lines += table(
        ("Time (UTC)", "Direction", "tts_playing", "ducked"),
        [(iso_time(row["epoch_seconds"]), row["dir"],
          str(row["tts_playing"]).lower(), str(row["ducked"]).lower())
         for row in yellow],
    )
    if not yellow:
        lines.append("No changes occurred during a matching track.")
    mapped = sum(valid_position(row) for row in adjustments)
    lines += [
        "",
        f"The maps show {mapped} of {len(adjustments)} adjustments with valid "
        "nearby GPS positions. A GPS match must be within 15 seconds; ride "
        "membership is determined independently from ride start/end intervals.",
        "Speed uses the logged reading when its age is at most 15 seconds, "
        "otherwise the nearby GPS speed. Mic readings require age at most "
        "five seconds. Missing readings are excluded from means.",
        "",
    ]
    return "\n".join(lines)


def write_csv(path, rows):
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            record = {}
            for field in CSV_FIELDS:
                value = row.get(field)
                if isinstance(value, (dict, list)):
                    value = json.dumps(value, ensure_ascii=False, sort_keys=True)
                record[field] = value
            writer.writerow(record)


def downsample(points, maximum=2000):
    if len(points) <= maximum:
        return points
    return [
        points[index * (len(points) - 1) // (maximum - 1)]
        for index in range(maximum)
    ]


def map_data(points, adjustments):
    grouped = defaultdict(list)
    for point in points:
        if point["ride_id"] is not None and valid_position(point):
            grouped[point["ride_id"]].append([point["lat"], point["lon"]])
    tracks = [
        {"ride_id": ride_id, "points": downsample(coordinates)}
        for ride_id, coordinates in grouped.items()
    ]
    markers = [
        {
            "lat": row["lat"],
            "lon": row["lon"],
            "time": iso_time(row["epoch_seconds"]),
            "delta": row["delta"],
            "speed": row["analysis_speed_mps"],
            "title": row["title"],
            "tts": row["tts_playing"],
            "ducked": row["ducked"],
            "ride_id": row["ride_id"],
        }
        for row in adjustments if valid_position(row)
    ]
    return {"tracks": tracks, "markers": markers}


HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Volume adjustments</title>
<link rel="stylesheet"
 href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>
html, body { height: 100%; margin: 0; font-family: sans-serif; }
body { display: flex; flex-direction: column; }
header { padding: 10px 14px; background: white; line-height: 1.5; }
#map { flex: 1; min-height: 300px; }
.dot { display: inline-block; width: 10px; height: 10px;
       border-radius: 50%; margin: 0 4px 0 12px; }
.up { background: #dc2626; } .down { background: #2563eb; }
.zero { background: #777; }
.leaflet-popup-content div { margin-bottom: 4px; }
</style>
</head>
<body>
<header>
<strong>Volume adjustments</strong>
<span class="dot up"></span>Net up
<span class="dot down"></span>Net down
<span class="dot zero"></span>Net zero
<br>Grey lines: rides. Radius: 4 + 2 × absolute net steps.
Context comes from the first change in each adjustment.
</header>
<div id="map"></div>
<script id="volume-data" type="application/json">__DATA__</script>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
"use strict";
const data = JSON.parse(document.getElementById("volume-data").textContent);
const map = L.map("map").setView([0, 0], 2);
L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
}).addTo(map);
const tracks = L.featureGroup().addTo(map);
for (const track of data.tracks) {
  L.polyline(track.points, {
    color: "#888", weight: 1, opacity: 0.55
  }).addTo(tracks);
}
const markers = L.featureGroup().addTo(map);
for (const item of data.markers) {
  const color = item.delta > 0 ? "#dc2626" :
                item.delta < 0 ? "#2563eb" : "#777";
  const popup = document.createElement("div");
  const speed = item.speed === null ? "unknown" : item.speed.toFixed(2) + " m/s";
  const fields = [
    ["Time (UTC)", item.time],
    ["Delta", (item.delta > 0 ? "+" : "") + item.delta + " steps"],
    ["Speed", speed],
    ["Track", item.title || "unknown"],
    ["TTS", String(item.tts)],
    ["Ducked", String(item.ducked)],
    ["Ride", item.ride_id === null ? "unknown" : String(item.ride_id)]
  ];
  for (const [label, value] of fields) {
    const line = document.createElement("div");
    line.textContent = label + ": " + value;
    popup.appendChild(line);
  }
  L.circleMarker([item.lat, item.lon], {
    color, fillColor: color, fillOpacity: 0.7, weight: 1,
    radius: 4 + 2 * Math.abs(item.delta)
  }).bindPopup(popup).addTo(markers);
}
if (markers.getLayers().length) {
  map.fitBounds(markers.getBounds().pad(0.12), {maxZoom: 16});
} else if (tracks.getLayers().length) {
  map.fitBounds(tracks.getBounds().pad(0.12), {maxZoom: 16});
}
</script>
</body>
</html>
"""


def write_html(path, data):
    embedded = json.dumps(data, ensure_ascii=False, allow_nan=False)
    # Keep database text from terminating the JSON script element.
    for character, replacement in (
        ("<", r"\u003c"), (">", r"\u003e"), ("&", r"\u0026"),
        ("\u2028", r"\u2028"), ("\u2029", r"\u2029"),
    ):
        embedded = embedded.replace(character, replacement)
    path.write_text(HTML_TEMPLATE.replace("__DATA__", embedded), encoding="utf-8")


def write_png(path, data):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("Skipping heatmap.png: matplotlib is unavailable.", file=sys.stderr)
        return False

    latitudes = [marker["lat"] for marker in data["markers"]]
    if not latitudes:
        latitudes = [
            point[0] for track in data["tracks"] for point in track["points"]
        ]
    reference_latitude = statistics.mean(latitudes) if latitudes else 0
    longitude_scale = max(0.01, abs(math.cos(math.radians(reference_latitude))))
    figure, axes = plt.subplots(figsize=(12, 9))
    try:
        for track in data["tracks"]:
            axes.plot(
                [point[1] * longitude_scale for point in track["points"]],
                [point[0] for point in track["points"]],
                color="#888888", linewidth=0.6, alpha=0.55, zorder=1,
            )
        for sign, color, label in (
            (1, "#dc2626", "Net up"),
            (-1, "#2563eb", "Net down"),
            (0, "#777777", "Net zero"),
        ):
            selected = [
                marker for marker in data["markers"]
                if (1 if marker["delta"] > 0 else
                    -1 if marker["delta"] < 0 else 0) == sign
            ]
            if selected:
                axes.scatter(
                    [marker["lon"] * longitude_scale for marker in selected],
                    [marker["lat"] for marker in selected],
                    s=[(2 * (4 + 2 * abs(marker["delta"]))) ** 2
                       for marker in selected],
                    c=color, alpha=0.7, linewidths=0.5,
                    edgecolors=color, label=label, zorder=2,
                )
        if data["markers"]:
            xs = [marker["lon"] * longitude_scale for marker in data["markers"]]
            ys = [marker["lat"] for marker in data["markers"]]
            x_padding = max((max(xs) - min(xs)) * 0.06, 0.001)
            y_padding = max((max(ys) - min(ys)) * 0.06, 0.001)
            axes.set_xlim(min(xs) - x_padding, max(xs) + x_padding)
            axes.set_ylim(min(ys) - y_padding, max(ys) + y_padding)
            axes.legend()
        elif not data["tracks"]:
            axes.text(
                0.5, 0.5, "No mapped adjustments or ride tracks",
                transform=axes.transAxes, ha="center", va="center",
            )
        axes.set_aspect("equal", adjustable="box")
        axes.set_xlabel(
            f"Longitude × cos({reference_latitude:.2f}°), scaled degrees"
        )
        axes.set_ylabel("Latitude (degrees)")
        axes.set_title("Volume adjustments — grey lines show rides")
        axes.grid(alpha=0.2)
        figure.tight_layout()
        figure.savefig(path, dpi=160)
    finally:
        plt.close(figure)
    return True


def main():
    server = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pantheon-db", default="Q:/Pantheon/data/pantheon.db")
    parser.add_argument("--audiplex-db", default=str(server / "audiplex.db"))
    parser.add_argument("--since", default="2026-10-01", help="ISO UTC cutoff")
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--owner", default="admin")  # settings.dj_owner_username
    parser.add_argument("--diag-log", default=str(server / "data" / "playback-diag.jsonl"))
    parser.add_argument("--include-app", action="store_true")
    args = parser.parse_args()
    try:
        since_epoch = epoch(args.since)
        since = iso_time(since_epoch)
    except (TypeError, ValueError, OverflowError, OSError):
        parser.error("--since must be a valid ISO date or timestamp")

    try:
        pantheon_path = Path(args.pantheon_db).expanduser().resolve().as_posix()
        audiplex_path = Path(args.audiplex_db).expanduser().resolve().as_posix()
        with closing(open_db(pantheon_path)) as pantheon:
            rows = load_changes(pantheon, since, args.include_app)
            points, rides = load_gps(pantheon)
        with closing(open_db(audiplex_path)) as audiplex:
            timeline, library, played, owner_found = load_tracks(
                audiplex, args.owner, since
            )
            diag_timeline = load_diag_timeline(args.diag_log, audiplex)
        rows.library_lufs = library
        rows.played_track_ids = played
        rows.owner_found = owner_found
        rows.owner = args.owner
        rows.since = since
        join_changes(rows, points, rides, timeline, diag_timeline)
        adjustments = collapse_adjustments(rows)
        data = map_data(points, adjustments)

        out_dir = Path(args.out_dir).expanduser().resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        summary_path = out_dir / "summary.md"
        csv_path = out_dir / "changes.csv"
        html_path = out_dir / "heatmap.html"
        png_path = out_dir / "heatmap.png"
        database_paths = {Path(pantheon_path), Path(audiplex_path)}
        if any(path.resolve() in database_paths
               for path in (summary_path, csv_path, html_path, png_path)):
            raise ValueError("An output path would overwrite an input database")

        summary_path.write_text(summarize(rows), encoding="utf-8")
        write_csv(csv_path, rows)
        write_html(html_path, data)
        outputs = [summary_path, csv_path, html_path]
        if write_png(png_path, data):
            outputs.append(png_path)
    except (sqlite3.Error, OSError, ValueError) as error:
        parser.exit(1, f"volume_analysis: {error}\n")

    for path in outputs:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

