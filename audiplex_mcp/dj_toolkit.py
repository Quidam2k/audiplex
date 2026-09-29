"""DJ toolkit (#2806): see and edit the upcoming queue, pool lanes, and bans.

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
               dj_ban, dj_unban, dj_bans):
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
