"""MCP tools for themed music buckets (#5518). Store: audiplex_mcp/buckets.py.

A bucket becomes a DJ lane through the shared resolver (source kind 'bucket'),
so dj_bucket_load is just dj_spec_add with a bucket source: the pool's lane
and starvation rules apply to it like any folder or artist.
"""

from pathlib import Path, PurePath

from audiplex_mcp import buckets

RIDE_SET = "todd-ride-mix"  # #4054
LUFS_QUEUE = Path(__file__).resolve().parents[1] / "server" / "data" / "lufs_priority_ids.txt"  # #7387

_NS: dict | None = None


def register(mcp, ns: dict) -> None:
    """Register the bucket tools without importing server.py (it runs as __main__)."""
    global _NS
    _NS = ns
    for fn in (dj_bucket_list, dj_bucket_show, dj_bucket_load, dj_bucket_save, dj_bucket_edit):
        mcp.tool()(fn)


def _helper(name: str):
    """Get a server helper at call time so tests can replace it."""
    if _NS is None:
        raise RuntimeError("bucket_tools.register() has not been called")
    return _NS[name]


def _find(bucket: str) -> tuple[dict | None, str]:
    try:
        b = buckets.find_bucket(bucket)
    except ValueError as e:
        return None, f"ERROR: {e}"
    if b is None:
        names = ", ".join(x["name"] for x in buckets.list_buckets()) or "none yet"
        return None, f"ERROR: No bucket matching '{bucket}'. Buckets: {names}."
    return b, ""


async def dj_bucket_list() -> str:
    """List the themed music buckets (themes, moods, occasions) a DJ can load as a lane.

    origin 'planned' = seeded ahead of time; 'dj' = built by a DJ persona.
    Load one onto a mix spec with dj_bucket_load.
    """
    rows = buckets.list_buckets()
    if not rows:
        return "No buckets yet. Build one with dj_bucket_save."
    lines = [f"{len(rows)} bucket(s):"]
    for r in rows:
        desc = f": {r['description']}" if r["description"] else ""
        lines.append(f"  - {r['name']} ({r['track_count']} tracks, {r['origin']}){desc}")
    return "\n".join(lines)


async def dj_bucket_show(bucket: str) -> str:
    """Show one bucket's description and its tracks in order (id and file name)."""
    b, err = _find(bucket)
    if err:
        return err
    lines = [f"Bucket '{b['name']}' ({b['origin']}, {len(b['tracks'])} tracks)"]
    if b["description"]:
        lines.append(f"  {b['description']}")
    for t in b["tracks"]:
        name = PurePath(t["path"]).name if t["path"] else ""
        note = f" [{t['note']}]" if t["note"] else ""
        lines.append(f"  - {t['track_id']} {name}{note}")
    return "\n".join(lines)


async def dj_bucket_load(bucket: str, spec: str, label: str = "") -> str:
    """Drop a bucket into a DJ mix spec as its own lane (re-syncs the live pool if active).

    bucket: bucket name (exact, or a unique part of it)
    spec:   the mix spec (scratch pad) to add it to, as named in dj_spec_list
    label:  lane label; default 'bucket: <name>'
    """
    b, err = _find(bucket)
    if err:
        return err
    if not b["tracks"]:
        return f"REFUSED: bucket '{b['name']}' is empty."
    source = {"kind": "bucket", "query": b["name"], "label": label or f"bucket: {b['name']}"}
    return await _helper("dj_spec_add")(spec=spec, add_sources=[source])


async def dj_bucket_save(
    name: str,
    description: str = "",
    track_ids: list[int] | None = None,
    sources: list[dict] | None = None,
    origin: str = "dj",
    created_by: str = "",
    replace: bool = False,
    add_to_set: str = RIDE_SET,
) -> str:
    """Build or extend a themed bucket. DJs use this to keep their own sets ride over ride.

    name:        bucket name, e.g. 'bravery', 'sunset on the river'
    track_ids:   specific tracks to add
    sources:     [{kind, query}] resolved like dj_mix sources (artist, album, genre,
                 folder, folder_match, playlist, favorites, bucket); every track is added
    origin:      'dj' (built by a persona, default) or 'planned'
    created_by:  who built it (jarvis, karen, orolo)
    replace:     True clears the bucket's tracks first; otherwise tracks are appended
                 and duplicates skipped
    add_to_set:  the set (saved spec) this bucket joins as its own lane, default
                 'todd-ride-mix' (#4054, Todd 10/8: a new bucket joins the mix, it never
                 replaces it). A live pool only gains the lane; nothing queued is cut.
                 Pass "" to keep the bucket out of every set.
    """
    tracks: list[dict] = [{"track_id": t} for t in track_ids or []]
    for src in sources or []:
        kind, query = str(src.get("kind", "folder")), str(src.get("query", ""))
        try:
            _label, found = await _helper("_resolve_source")(kind, query)
        except LookupError as e:
            return f"ERROR: {e}"
        tracks += [
            {"track_id": int(t["id"]), "path": t.get("path") or t.get("file_path") or ""}
            for t in found
        ]
    try:
        r = buckets.save_bucket(name, description, origin, created_by, tracks, replace)
    except ValueError as e:
        return f"ERROR: {e}"
    verb = "Created" if r["created"] else "Updated"
    dup = f", {r['skipped_duplicates']} already in it" if r["skipped_duplicates"] else ""
    out = f"{verb} bucket '{r['name']}': +{r['added']} tracks{dup}, {r['track_count']} total."
    _queue_for_loudness(t["track_id"] for t in tracks)  # #7387 measured before it plays
    if add_to_set:
        out += " " + await _join_set(r["name"], add_to_set)
    return out


def _queue_for_loudness(track_ids) -> None:
    """#7387: put new bucket tracks at the front of the idle LUFS run."""
    try:
        ids = {int(t) for t in track_ids}
        old = {int(x) for x in LUFS_QUEUE.read_text().split(",") if x.strip()} if LUFS_QUEUE.exists() else set()
        if ids - old:
            LUFS_QUEUE.write_text(",".join(map(str, sorted(old | ids))))
    except (OSError, ValueError):
        pass


async def _join_set(bucket_name: str, set_name: str) -> str:
    """#4054: add the bucket as a lane of the set unless it is already one."""
    spec = await _helper("_get_spec")(set_name)
    if isinstance(spec, str):
        return f"Not added to a set: {spec}"
    if any(s.get("kind") == "bucket" and s.get("query") == bucket_name for s in spec.get("sources") or []):
        return f"Already a lane of set '{set_name}'."
    source = {"kind": "bucket", "query": bucket_name, "label": f"bucket: {bucket_name}"}
    return await _helper("dj_spec_add")(spec=set_name, add_sources=[source], replan=False)


async def dj_bucket_edit(
    bucket: str,
    add_track_ids: list[int] | None = None,
    remove_track_ids: list[int] | None = None,
    added_by: str = "",
) -> str:
    """Add or remove specific tracks in an existing bucket (refine it after a ride)."""
    b, err = _find(bucket)
    if err:
        return err
    try:
        out = []
        if add_track_ids:
            r = buckets.add_tracks(b["name"], add_track_ids, added_by)
            out.append(f"+{r['added']}")
        if remove_track_ids:
            r = buckets.remove_tracks(b["name"], remove_track_ids)
            out.append(f"-{r['removed']}")
    except ValueError as e:
        return f"ERROR: {e}"
    if not out:
        return "Nothing to change: pass add_track_ids and/or remove_track_ids."
    return f"Bucket '{b['name']}': {' '.join(out)}, {r['track_count']} total."
