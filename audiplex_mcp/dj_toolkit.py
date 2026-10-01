"""DJ toolkit (#2806): the upcoming queue, pool lanes, bans, tags and energy sets.

Every queue edit reads the device's queue, computes the new tail here, and
sends ONE replace_upcoming: the current song is never touched and never
restarted. An old phone build without replace_upcoming gets the #5463
boundary swap instead (the new tail goes in when the current song ends).
The server owns the pool and the ban list, so those go over HTTP only.
"""

import asyncio

_NS: dict | None = None


def register(mcp, ns: dict) -> None:
    """Register the toolkit tools without importing server.py (it runs as __main__)."""
    global _NS
    _NS = ns
    for fn in (dj_upcoming, dj_remove, dj_insert, dj_swap, dj_pool_lane,
               dj_ban, dj_unban, dj_bans, dj_tag, dj_untag, dj_tags, dj_energy_set,
               dj_harmonic_set, dj_crossfade, dj_star, dj_resume):  # #3576 dj_star, #3601 dj_resume
        mcp.tool()(fn)


def _h(name: str):
    """A server helper, looked up at call time so tests can replace it."""
    if _NS is None:
        raise RuntimeError("dj_toolkit.register() has not been called")
    return _NS[name]


async def _music_queue() -> tuple[dict, list[dict], int, str]:
    """(state, queue, current index, refusal). refusal is '' when editable."""
    try:
        state = await _h("_get")("/api/playback/state")
    except PermissionError as e:
        return {}, [], 0, str(e)
    track = state.get("track") or {}
    queue = state.get("queue") or []
    idx = int(state.get("queue_index") or 0)
    if state.get("book"):
        return state, queue, idx, "An audiobook is playing; there's no music queue to edit."
    if track.get("id") == -1 and not queue:
        return state, queue, idx, "A live stream is playing; there's no music queue to edit."
    if track.get("id") is None or not queue:
        return state, queue, idx, "Nothing is loaded, so there's no queue to edit. Start music first."
    length = int(state.get("queue_length") or 0)
    last = max(int(q.get("index", 0)) for q in queue)
    if length and last < length - 1:
        return state, queue, idx, (
            f"The device reported only up to #{last} of {length} queued; editing now would drop "
            "the rest. Nothing sent.")
    return state, queue, idx, ""


def _tail(queue: list[dict], idx: int) -> list[dict]:
    return [q for q in queue if int(q.get("index", 0)) > idx]


async def _send_tail(state: dict, ids: list[int]) -> str:
    """Replace everything after the current song with `ids`."""
    if await _h("_device_lacks_replace_upcoming")():
        if not state.get("playing"):
            return ("Old phone build can't edit the queue in place and the player is paused;"
                    " a swap would start music, so NOTHING was sent.")
        ns = _NS
        old = ns["_SWAP"].get("task")
        if old is not None and not old.done():
            old.cancel()
        ns["_SWAP"]["ids"] = list(ids)
        rem = _h("_remaining_ms")(state)
        wait = max(ns["SWAP_MIN_WAIT_S"], (rem or 0) / 1000 + 60)
        cur = int((state.get("track") or {}).get("id") or 0)
        ns["_SWAP"]["task"] = asyncio.create_task(_h("_boundary_swap")(list(ids), cur, wait))
        _h("_swap_set")("pending", f"{len(ids)} track(s) replace the queue when the current song ends")
        return ("Old phone build: the edited queue goes in when the current song ends"
                " (never mid-song). Check dj_mix_status().")
    chunk = _NS["QUEUE_CHUNK"]
    first, rest = ids[:chunk], ids[chunk:]
    data = await _h("_enqueue")("replace_upcoming", {"track_ids": first})
    if isinstance(data, str):
        return _h("_held_result")(data)
    ack = await _h("_await_ack")(int(data["id"]))
    if ack is None:
        return (f"Sent replace_upcoming (command #{data['id']}); no ack yet, check dj_command_status."
                + (f" {len(rest)} more track(s) NOT sent yet." if rest else ""))
    if ack.get("ack_status") not in ("ok", "partial"):
        return f"The device refused it: {ack.get('ack_status')} {ack.get('ack_detail') or ''}".rstrip()
    if rest:
        more = await _h("_enqueue")("queue", {"track_ids": rest})
        if isinstance(more, str):
            return f"First {len(first)} sent (command #{data['id']}), the other {len(rest)} held: {more}"
    note = _h("_missing_note")(data)
    return f"Done (command #{data['id']}, {ack.get('ack_status')}).{note}"


def _line(q: dict, source_of: dict[int, str], idx: int) -> str:
    tid = int(q.get("id") or 0)
    mark = ">" if int(q.get("index", 0)) == idx else " "
    who = f" - {q['artist']}" if q.get("artist") else ""
    src = f"  [{source_of[tid]}]" if tid in source_of else ""
    kind = "  (DJ clip)" if tid < 0 else ""
    return f"{mark}#{q.get('index')}  id {tid}  {q.get('title') or '?'}{who}{src}{kind}"


async def dj_upcoming(n: int = 20) -> str:
    """What plays next, in order, with each track's queue index and mix source.

    The '#N' index is what dj_remove / dj_insert / dj_swap / dj_reorder take.
    '>' marks the song playing now.
    """
    state, queue, idx, refusal = await _music_queue()
    if refusal:
        return refusal
    source_of = _h("_load_source_map")()
    tail = _tail(queue, idx)
    cur = [q for q in queue if int(q.get("index", 0)) == idx]
    lines = [f"{len(tail)} track(s) after the current one"
             + (f", showing {min(n, len(tail))}" if len(tail) > n else "") + ":"]
    lines += [_line(q, source_of, idx) for q in cur + tail[: max(0, n)]]
    return "\n".join(lines)


def _check_indexes(indexes: list[int], idx: int, tail_idx: set[int]) -> str:
    bad_cur = [i for i in indexes if i <= idx]
    if bad_cur:
        return (f"#{bad_cur[0]} is the song playing now or already played; queue edits only touch "
                "what's still to come. Use dj_skip to leave the current song.")
    missing = [i for i in indexes if i not in tail_idx]
    if missing:
        return f"No queue entry #{missing[0]}. Check dj_upcoming()."
    return ""


def _clip_refusal(tail: list[dict]) -> str:
    if any(int(q.get("id") or 0) < 0 for q in tail):
        return ("A DJ clip is queued ahead and can't be re-sent, so the queue can't be rewritten"
                " right now. Wait for the clip to play, then edit.")
    return ""


async def dj_remove(indexes: list[int] | None = None, track_ids: list[int] | None = None) -> str:
    """Take tracks out of the upcoming queue, by '#N' index (dj_upcoming) or by track id.

    The current song keeps playing; only what's still to come changes.
    """
    state, queue, idx, refusal = await _music_queue()
    if refusal:
        return refusal
    tail = _tail(queue, idx)
    drop_idx = set(int(i) for i in indexes or [])
    err = _check_indexes(sorted(drop_idx), idx, {int(q["index"]) for q in tail})
    if err:
        return err
    drop_ids = set(int(i) for i in track_ids or [])
    kept = [q for q in tail if int(q["index"]) not in drop_idx and int(q["id"]) not in drop_ids]
    removed = len(tail) - len(kept)
    if not removed:
        return "None of those are in the upcoming queue; nothing sent."
    err = _clip_refusal(tail)
    if err:
        return err
    return f"Removing {removed} track(s). " + await _send_tail(state, [int(q["id"]) for q in kept])


async def dj_insert(track_ids: list[int], at_index: int = -1) -> str:
    """Put tracks into the upcoming queue at '#at_index' (they take that slot and
    push the rest down). -1 = the end. Use dj_play_next for "right after this song"."""
    if not track_ids:
        return "Give track_ids to insert."
    state, queue, idx, refusal = await _music_queue()
    if refusal:
        return refusal
    tail = _tail(queue, idx)
    ids = [int(q["id"]) for q in tail]
    if at_index == -1:
        pos = len(ids)
    else:
        if at_index <= idx:
            return _check_indexes([at_index], idx, set())
        pos = at_index - idx - 1
        if pos > len(ids):
            return f"No queue slot #{at_index}; the queue ends at #{idx + len(ids)}. Use -1 for the end."
    err = _clip_refusal(tail)
    if err:
        return err
    new = ids[:pos] + [int(t) for t in track_ids] + ids[pos:]
    return f"Inserting {len(track_ids)} track(s) at #{idx + 1 + pos}. " + await _send_tail(state, new)


async def dj_swap(index: int, track_ids: list[int]) -> str:
    """Replace the upcoming track at '#index' with track_ids (one or more)."""
    if not track_ids:
        return "Give track_ids to swap in."
    state, queue, idx, refusal = await _music_queue()
    if refusal:
        return refusal
    tail = _tail(queue, idx)
    err = _check_indexes([index], idx, {int(q["index"]) for q in tail}) or _clip_refusal(tail)
    if err:
        return err
    new: list[int] = []
    for q in tail:
        if int(q["index"]) == index:
            new.extend(int(t) for t in track_ids)
        else:
            new.append(int(q["id"]))
    return f"Swapping #{index}. " + await _send_tail(state, new)


async def dj_pool_lane(lane: str, action: str = "pause") -> str:
    """Pause, resume or remove one lane of the rolling pool (dj_pool_status lists lanes).

    A paused lane stops getting picks until resumed; remove drops it for this
    pool run. Tracks already queued stay queued (dj_remove them if needed).
    """
    try:
        res = await _h("_patch")("/api/playback/pool/lanes", {"lane": lane, "action": action})
    except PermissionError as e:
        return str(e)
    except Exception as e:  # 400: unknown lane/action, message is sayable
        resp = getattr(e, "response", None)
        try:
            return f"Not changed: {resp.json().get('detail')}"
        except Exception:
            return f"Not changed: {e}"
    parts = [f"{ln['label']}{' (paused)' if ln.get('paused') else ''}" for ln in res.get("lanes") or []]
    return f"Lane '{res['lane']}' {action}d. Lanes now: {', '.join(parts) or 'none'}."


async def dj_ban(track_ids: list[int], reason: str = "", persona: str = "",
                 remove_from_queue: bool = True) -> str:
    """Never pick these tracks again (any copy of the same recording): mixes and
    the rolling pool skip them. Reversible with dj_unban; dj_bans lists them.
    A song Todd asks for by name still plays via dj_play_now.

    remove_from_queue: also take them out of the upcoming queue (default).
    """
    if not track_ids:
        return "Give track_ids to ban."
    try:
        res = await _h("_post")("/api/playback/bans",
                                {"track_ids": track_ids, "reason": reason or None,
                                 "persona": persona or None})
    except PermissionError as e:
        return str(e)
    out = f"Banned {len(res.get('banned') or [])}"
    if res.get("already"):
        out += f", {len(res['already'])} already banned"
    if res.get("unknown"):
        out += f", unknown id(s) {res['unknown']}"
    out += ". Undo with dj_unban."
    if remove_from_queue:
        state, queue, idx, refusal = await _music_queue()
        ids = set(int(t) for t in track_ids)
        if not refusal and any(int(q["id"]) in ids for q in _tail(queue, idx)):
            out += " " + await dj_remove(track_ids=list(ids))
    return out


async def dj_unban(track_ids: list[int]) -> str:
    """Lift a dj_ban; those tracks can be picked again."""
    try:
        res = await _h("_delete_json")("/api/playback/bans", {"track_ids": track_ids})
    except PermissionError as e:
        return str(e)
    out = f"Unbanned {len(res.get('unbanned') or [])}."
    if res.get("not_banned"):
        out += f" Not banned: {res['not_banned']}."
    return out


async def dj_bans() -> str:
    """Everything the DJ has banned, with who and why."""
    try:
        rows = await _h("_get")("/api/playback/bans")
    except PermissionError as e:
        return str(e)
    if not rows:
        return "No bans."
    lines = [f"{len(rows)} banned:"]
    for r in rows:
        who = f" - {r['artist']}" if r.get("artist") else ""
        why = f"  ({r['reason']})" if r.get("reason") else ""
        by = f" [{r['persona']}]" if r.get("persona") else ""
        lines.append(f"  id {r['track_id']}  {r.get('title') or '?'}{who}{why}{by}")
    return "\n".join(lines)


async def _say_http_error(e: Exception) -> str:
    resp = getattr(e, "response", None)
    try:
        return f"Not done: {resp.json().get('detail')}"
    except Exception:
        return f"Not done: {e}"


async def dj_tag(track_ids: list[int], tags: list[str], persona: str = "") -> str:
    """Put mood/vibe tags on tracks ("chill", "anthem", "rainy day"). Tags are
    yours to apply by ear; nothing is inferred. Use them as a mix/pool source
    ({"kind": "tag", "query": "chill"}) or to filter dj_energy_set.
    dj_untag removes them; dj_tags lists them."""
    if not track_ids or not tags:
        return "Give track_ids and tags."
    try:
        res = await _h("_post")("/api/playback/tags",
                                {"track_ids": track_ids, "tags": tags, "persona": persona or None})
    except PermissionError as e:
        return str(e)
    except Exception as e:
        return await _say_http_error(e)
    out = f"Tagged {len(res.get('tracks') or [])} track(s) {', '.join(res.get('tags') or [])} ({res.get('added', 0)} new)."
    if res.get("unknown"):
        out += f" Unknown id(s) {res['unknown']}."
    return out


async def dj_star(track_ids: list[int], stars: float, words: str = "", persona: str = "") -> str:
    """Set Todd's star rating (1-5, halves allowed) when he says one out loud:
    "five stars", "four and a half". It lands in the SAME star field he taps in
    the app, on his account, so he sees it there. #3576. Halves show as halves
    in app 1.0.51+; older app builds show the whole star below.

    track_ids: every copy of the song (dj_search; the same song can live twice).
    words: his own words, verbatim-ish ("one of the all-time greats") - they
        are kept with the rating and teach more than the number.
    persona: who heard it. Re-rating replaces. (#6117: halves are stored exactly.)
    Do not use the old five-star-verbal tags for this any more."""
    if not track_ids:
        return "Give track_ids (dj_search finds them)."
    try:
        res = await _h("_put")("/api/playback/ratings", {
            "track_ids": track_ids, "stars": stars, "words": words, "persona": persona})
    except PermissionError as e:
        return str(e)
    except Exception as e:
        return await _say_http_error(e)
    rated = res.get("rated") or []
    if not rated:
        return f"Nothing rated: unknown id(s) {res.get('unknown')}."
    changed = [f"{r['track_id']} (was {r['was']})" for r in rated if r.get("was") not in (None, r["rating"])]
    out = (f"Rated {len(rated)} track(s) {res['stored']:g} stars "  # #6117
           f"(older app builds show {int(res['stored'])}). Note: {res['note']!r}.")
    if changed:
        out += f" Changed: {', '.join(changed)}."
    if res.get("unknown"):
        out += f" Unknown id(s) {res['unknown']}."
    return out


async def dj_resume(play: bool = False) -> str:
    """Put Todd's last music queue back on the phone, at the song and spot he
    left it (#3601). The queue lives only in the phone's memory, so after a
    pause plus the app being closed it is gone from the app; the server keeps
    a copy. Use when he says "put my music back" / "where was I".

    play=False (default): restore PAUSED. Nothing starts; he sees it in the
        app and hits play. Phones older than 1.0.49 can't do a silent restore
        and will say so; then offer play=True.
    play=True: restore and start playing now (talk-guarded like any start)."""
    try:
        snap = await _h("_get")("/api/playback/resume")
    except PermissionError as e:
        return str(e)
    except Exception as e:
        if getattr(getattr(e, "response", None), "status_code", None) == 404:
            return "No saved queue to put back: the phone hasn't reported one since this was added."
        return await _say_http_error(e)
    ids = list(snap.get("track_ids") or [])[int(snap.get("index") or 0):]
    if not ids:
        return "The saved queue has no tracks left after where he stopped."
    pos = int(snap.get("position_ms") or 0)
    what = " - ".join(x for x in (snap.get("artist"), snap.get("title")) if x) or f"track {ids[0]}"
    mins = round(float(snap.get("age_seconds") or 0) / 60)
    where = f"{what} at {pos // 60000}:{pos // 1000 % 60:02d}, {len(ids)} track(s) to go (saved {mins} min ago)"
    if play:
        data = await _h("_enqueue")("play_now", {"track_ids": ids})
        if not isinstance(data, dict):
            return data
        if pos > 5000:
            await _h("_enqueue")("seek", {"position_ms": pos})
        return f"Restored and playing: {where}."
    head, rest = ids[:40], ids[40:]
    data = await _h("_enqueue")("activate", {"track_ids": head, "position_ms": pos, "playing": False})
    if not isinstance(data, dict):
        return data
    ack = await _h("_await_ack")(data["id"], 12.0)
    if ack is None:
        return f"Sent the restore but the phone hasn't answered yet: {where}."
    if ack.get("ack_status") == "unknown_type":
        return ("This phone build can't restore without playing (needs 1.0.49 or later). "
                f"Nothing changed. Saved: {where}. dj_resume(play=True) restores and starts it.")
    if ack.get("ack_status") not in ("ok", "partial"):
        return f"Restore not done ({ack.get('ack_status')}: {ack.get('ack_detail') or ''}). Saved: {where}."
    if rest:
        more = await _h("_enqueue")("queue", {"track_ids": rest})
        if not isinstance(more, dict):
            return f"Restored paused: {where}, but the rest didn't queue: {more}"
    return f"Restored, paused, in the app: {where}. He presses play to go on."


async def dj_untag(track_ids: list[int], tags: list[str] | None = None) -> str:
    """Take tags off tracks. No tags = every tag on those tracks."""
    try:
        res = await _h("_delete_json")("/api/playback/tags", {"track_ids": track_ids, "tags": tags or []})
    except PermissionError as e:
        return str(e)
    return f"Removed {res.get('removed', 0)} tag(s)."


async def dj_tags(tag: str = "") -> str:
    """No tag: every DJ tag with its track count. With a tag: the tracks carrying
    it, with measured energy (0-100, '?' = not measured yet)."""
    try:
        if not tag:
            rows = await _h("_get")("/api/playback/tags")
            if not rows:
                return "No tags yet. dj_tag(track_ids, tags) adds some."
            return "Tags: " + ", ".join(f"{r['tag']} ({r['count']})" for r in rows)
        from urllib.parse import quote

        rows = await _h("_get")(f"/api/playback/tags/{quote(tag.strip(), safe='')}")
    except PermissionError as e:
        return str(e)
    if not rows:
        return f"No tracks tagged '{tag}'."
    lines = [f"{len(rows)} tagged '{tag}':"]
    for r in rows:
        who = f" - {r['artist']}" if r.get("artist") else ""
        e = r.get("energy")
        lines.append(f"  id {r['track_id']}  {r.get('title') or '?'}{who}  energy {'?' if e is None else e}")
    return "\n".join(lines)


async def _collect_ids(sources, track_ids, exclude_recent_hours) -> tuple:
    """(ids, recent note) from sources + explicit ids, or (refusal str, '')."""
    ids: list[int] = [int(t) for t in track_ids or []]
    empty: list[str] = []
    for src in sources or []:
        try:
            _label, tracks = await _h("_resolve_source")(
                str(src.get("kind", "folder")), str(src.get("query", "")),
                recursive=bool(src.get("recursive", True)))
        except PermissionError as e:
            return str(e), ""
        except Exception as e:  # LookupError / unknown folder: name it
            tracks, why = [], f" ({e})"
        else:
            why = ""
        if not tracks:
            empty.append(f"{src.get('kind', 'folder')} '{src.get('query', '')}'{why}")
        ids += [int(t["id"]) for t in tracks]
    if empty:
        return "REFUSED, nothing sent: " + "; ".join(f"{x}: 0 tracks" for x in empty) + ".", ""
    if not ids:
        return "Give sources or track_ids for the set.", ""
    recent = ""
    if exclude_recent_hours:
        kept, dropped = await _h("_recent_split")(ids, exclude_recent_hours)
        if kept:
            ids = kept
            recent = f" Left out {dropped} heard in the last {exclude_recent_hours:g}h." if dropped else ""
    return ids, recent


async def dj_energy_set(
    arc: str,
    sources: list[dict] | None = None,
    track_ids: list[int] | None = None,
    minutes: float = 0,
    tags: list[str] | None = None,
    min_energy: int | None = None,
    max_energy: int | None = None,
    seed: int | None = None,
    exclude_recent_hours: float = 12,
) -> str:
    """Build a set that follows an energy arc, from MEASURED energy (0-100).

    arc:      rise (low to high) | peak (builds, tops out ~2/3 in, comes down)
              | wind_down (high to low) | steady (near the middle, shuffled).
    sources:  same as dj_mix ([{"kind": "folder", "query": ...}], kinds incl.
              'tag' and 'search'); track_ids: explicit ids as well.
    minutes:  fill about this long (0 = every candidate).
    tags:     keep only tracks carrying ALL these DJ tags.
    min_energy / max_energy: an energy window.

    It REPLACES what's queued after the current song (never the current song)
    and stops the rolling pool, like dj_mix. Tracks without a measured energy
    are left out and counted: the analyzer (server/scripts/measure_energy.py)
    fills them in a quiet hour, never during a ride.
    """
    ids, recent = await _collect_ids(sources, track_ids, exclude_recent_hours)
    if isinstance(ids, str):
        return ids
    body = {"track_ids": ids, "arc": arc, "minutes": minutes, "tags": tags or [], "seed": seed,
            "min_energy": min_energy, "max_energy": max_energy}
    try:
        res = await _h("_post")("/api/playback/energy/arc", body)
    except PermissionError as e:
        return str(e)
    except Exception as e:
        return await _say_http_error(e)
    ordered = res.get("ordered") or []
    why = []
    if res.get("unmeasured"):
        why.append(f"{res['unmeasured']} not measured yet")
    if res.get("untagged"):
        why.append(f"{res['untagged']} without the tags")
    if res.get("out_of_range"):
        why.append(f"{res['out_of_range']} outside the energy window")
    if res.get("dropped"):
        why.append(f"{res['dropped']} banned/non-music/too long")
    left_out = f" Left out: {', '.join(why)}." if why else ""
    if not ordered:
        return "Nothing fits that set, nothing sent." + left_out
    e = res.get("energies") or []
    head = (f"{arc} set: {len(ordered)} track(s), ~{res.get('minutes')} min, energy "
            f"{e[0]} -> {max(e)} -> {e[-1]}.{left_out}{recent} ")
    return head + await _h("dj_mix")(track_ids=ordered, shuffle=False, keep_upcoming=False,
                                     exclude_recent_hours=0)


async def dj_harmonic_set(
    sources: list[dict] | None = None,
    track_ids: list[int] | None = None,
    minutes: float = 0,
    start_track_id: int | None = None,
    bpm_tolerance: float = 0.06,
    arc: str | None = None,
    seed: int | None = None,
    exclude_recent_hours: float = 12,
) -> str:
    """Build a set where each song hands off in a compatible KEY near the same TEMPO.

    Uses MEASURED tempo (BPM) and key (tracks.bpm / musical_key, Camelot codes:
    8B = C major, 8A = A minor; same number or one step round the wheel mixes
    smoothly). Half/double time counts as a tempo match.
    sources / track_ids: as dj_energy_set. minutes: fill about this long (0 = all).
    start_track_id: open with this track. bpm_tolerance: 0.06 = within 6%.
    arc: optionally also follow an energy arc (rise / peak / wind_down / steady).

    It REPLACES what's queued after the current song (never the current song),
    like dj_energy_set. Tracks without a measured tempo and key are left out and
    counted: server/scripts/measure_tempo_key.py fills them in a quiet hour.
    """
    ids, recent = await _collect_ids(sources, track_ids, exclude_recent_hours)
    if isinstance(ids, str):
        return ids
    body = {"track_ids": ids, "minutes": minutes, "start_track_id": start_track_id,
            "bpm_tolerance": bpm_tolerance, "arc": arc, "seed": seed}
    try:
        res = await _h("_post")("/api/playback/harmonic/order", body)
    except PermissionError as e:
        return str(e)
    except Exception as e:
        return await _say_http_error(e)
    ordered = res.get("ordered") or []
    why = []
    if res.get("unanalysed"):
        why.append(f"{res['unanalysed']} without a measured tempo/key yet")
    if res.get("dropped"):
        why.append(f"{res['dropped']} banned/non-music/too long")
    left_out = f" Left out: {', '.join(why)}." if why else ""
    if not ordered:
        return "Nothing fits that set, nothing sent." + left_out
    keys, bpms = res.get("keys") or [], res.get("bpms") or []
    hops = len(ordered) - 1
    path = " > ".join(f"{k} {round(b)}" for k, b in list(zip(keys, bpms))[:6])
    head = (f"Harmonic set: {len(ordered)} track(s), ~{res.get('minutes')} min, "
            f"{res.get('smooth', 0)} of {hops} hand-offs key- and tempo-compatible. "
            f"Opens {path}{' ...' if len(ordered) > 6 else ''}.{left_out}{recent} ")
    return head + await _h("dj_mix")(track_ids=ordered, shuffle=False, keep_upcoming=False,
                                     exclude_recent_hours=0)


async def dj_crossfade(seconds: float = 6) -> str:
    """Overlap the end of each song with the start of the next (0 = off, max 12 s).

    PC speaker renderer only: the phone doesn't crossfade yet and says so.
    Only song -> song: books, streams, DJ clips and a paused player never
    crossfade. The setting lasts until the PC app restarts.
    """
    data = await _h("_enqueue")("set_crossfade", {"seconds": max(0.0, min(12.0, float(seconds)))})
    if isinstance(data, str):
        return _h("_held_result")(data)
    ack = await _h("_await_ack")(int(data["id"]))
    if ack is None:
        return f"Sent (command #{data['id']}); no ack yet, check dj_command_status."
    if ack.get("ack_status") == "unknown_type":
        return "This device can't crossfade (only the PC speaker renderer can). Nothing changed."
    if ack.get("ack_status") != "ok":
        return f"The device refused it: {ack.get('ack_status')} {ack.get('ack_detail') or ''}".rstrip()
    return f"Crossfade {'off' if not seconds else ack.get('ack_detail') or 'on'}: applies from the next song change."
