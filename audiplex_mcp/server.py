"""Audiplex DJ — dedicated MCP server.

Exposes the Audiplex playback command bus as agent ("DJ") tools. Kept as a
standalone package (NOT folded into pantheon_mcp_server) so other Pantheon
adopters can run it independently against their own Audiplex instance.

Config via environment:
  AUDIPLEX_URL    base URL of the Audiplex server (e.g. http://100.x.y.z:8000)
  AUDIPLEX_TOKEN  service-account JWT — mint via:
                    cd server && python -m audiplex.create_service_token
                  When unset/empty, falls back to reading a `.dj_token` file
                  at the repo root (single source: rotation = re-mint +
                  overwrite that one file instead of editing every agent's
                  MCP config).

  DJ_TTS_URL      OpenAI-compatible speech endpoint for voice breaks — see
                  tts_backend.py for the full TTS config surface. Only
                  dj_announce needs it; the other tools work without it.

Tools: the full catalog, one line each and grouped, is audiplex_mcp/TOOLS.md
(#ride0928). server/tests/test_ride0928_tools_catalog.py fails if a registered
tool is missing from it, so add the line when you add a tool.

dj_bed_play/dj_bed_stop/dj_bed_volume/dj_sleep_timer/dj_cancel_sleep_timer/
dj_sleep_start are the sleep-engine lane (item #1728): a continuously-looping
ambient "bed" layer that runs on a second, independent device player
alongside whatever dj_play_now/dj_play_stream is doing on the main one, plus
a sleep-timer fade-out for the main layer. Two fixed layers (bed + main), not
a general mixer — see the section comment above dj_bed_play.

dj_recommend/dj_rate/dj_taste are the discovery + taste lane (item #2945): the
DJ proposes music the library DOESN'T have, Todd's spoken reaction is relayed
back through dj_rate, and dj_taste feeds the accumulated signal into later
picks. State lives in a local SQLite file (DJ_TASTE_DB, default
data/dj/taste.db) rather than audiplex.db — see the section comment above
dj_recommend for why the existing tables can't carry it.

dj_library/dj_tracks/dj_search are the catalog-browse lane (item #2943): they
let the agent survey the library and pick tracks UNPROMPTED instead of only
taking requests. They matter more than they look, because the live library is
a flat yt-dlp dump with no embedded tags — every track scanned into one album
under a nameless artist with no genres — so the artist/album/genre axes are
degenerate and only the track TITLES carry real information. dj_library says
so explicitly rather than letting an agent conclude the library is empty.
Browse is resolved MCP-side over the existing library-global catalog REST
(/api/music/folders, /folders/tracks, /artists, /albums, /genres, /roots);
there is still no server-side /search endpoint and none was added.

dj_break_brief and dj_announce are the DJ-persona lane (item #431):
dj_break_brief hands the agent a dayparted brief, the agent writes the copy,
dj_announce synthesizes and queues it as a voice break. The agent can pass
explicit track IDs or let dj_queue_by resolve an artist/album/genre/folder/
playlist/favorites NAME to tracks MCP-side. Playlist and favorites resolution go through /api/playback/
(owner-resolved reads), NOT /api/music/ — the latter is scoped to the
caller and dj-agent has none. dj_play_stream routes an external HTTP audio
stream (e.g. Radio Free Luna's /stream.mp3) to the device — see the
token-leak guard in the Android AuthInterceptor before assuming this is
safe to extend to other stream-carrying commands.
"""

import asyncio
import contextlib
import datetime
import json
import os
import re
import shutil
import sqlite3
import sys
import time  # #2843
from pathlib import Path
from urllib.parse import quote

import httpx
from mcp.server.fastmcp import FastMCP

from audiplex_mcp import dj_bridge_watcher, dj_persona, tts_backend  # #2858 dj_patter
from audiplex_mcp.mix_balance import balance_order, describe_head  # #5463

AUDIPLEX_URL = os.environ.get("AUDIPLEX_URL", "http://localhost:8000").rstrip("/")


def _load_token() -> str:
    """Env wins; otherwise fall back to the .dj_token file at the repo root."""
    token = os.environ.get("AUDIPLEX_TOKEN", "")
    if token:
        return token
    token_file = Path(__file__).resolve().parent.parent / ".dj_token"
    try:
        return token_file.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


AUDIPLEX_TOKEN = _load_token()

mcp = FastMCP("audiplex-dj")


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {AUDIPLEX_TOKEN}"}


@mcp.tool()
async def dj_play_now(track_ids: list[int]) -> str:
    """Immediately play the given music tracks on the Audiplex device,
    replacing the current queue.

    track_ids are Audiplex music track IDs — resolve names/albums/artists to
    IDs first via the catalog REST API (GET /api/music/albums, /artists, etc.).
    Tracks play in the order given.
    """
    if not track_ids:
        return "No track_ids given; nothing to play."

    # Stop any active pool (#5495, item 4) - via server HTTP DELETE
    pool_msg = ""
    try:
        pool_result = await _delete("/api/playback/pool")
        if pool_result.get("stopped"):
            pool_msg = " Stopped the rolling pool."
    except Exception:
        pass  # No pool running, or error communicating

    data = await _enqueue("play_now", {"track_ids": track_ids})  # #3249: gated
    if isinstance(data, str):
        return _held_result(data)  # #ride0928
    result = await _result(data, len(track_ids))  # #ride0928
    try:
        device = await _get("/api/playback/device")  # #ride0928: one HTTP path (stubbable)
    except Exception:
        device = {}
    head = (_missing_note(data) + (  # #3249
        f" Queued play_now for {_sent_count(data, len(track_ids))} track(s) "
        f"(command #{data.get('id')}, {data.get('pending')} pending). "
        f"Confirm with dj_command_status({data.get('id')}) — it reports whether "
        f"the device acked, which is the difference between 'sent' and 'played'. "
    )).lstrip()  # #3249
    head = result + head  # #ride0928
    # Never claim "it's playing" — say whether anything is listening, so a dead
    # player reads as a failure instead of a success (#2961).
    note = await _repeat_note(track_ids)
    if not _is_yes(result):  # #2843: no "should pick this up" over a NO/UNCONFIRMED
        return head + pool_msg + (" " + _describe_device(device) if device else "") + note
    if not device:
        return head + pool_msg + " The device plays when it next polls (immediately if awake)." + note
    if device.get("connected"):
        return (
            head
            + pool_msg
            + " A player is connected, so it should pick this up within seconds."
            + note
        )
    return (
        head
        + pool_msg
        + " " + _describe_device(device)
        + " It will play whenever a player next starts."
        + note
    )


@mcp.tool()
async def dj_command_status(command_id: int = 0, limit: int = 10) -> str:
    """Did the device actually take a DJ command, and what did it do with it?

    Pass a command_id for one command, or nothing for the recent few. This is
    the delivery receipt the 2026-08-14 test did not have: back then a command
    was destroyed by the poll that delivered it, so "queued" was the last thing
    anyone could say about it, and a command silently dropped by the app looked
    exactly like one still in flight.

    Statuses: queued (nobody has taken it), delivered (handed over, no answer
    yet — it is re-offered after 60s), acked (the device carried it out),
    failed (the device explicitly could not — see ack_detail).
    """
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{AUDIPLEX_URL}/api/playback/commands?limit={max(limit, 1)}",
            headers=_headers(),
        )
        if resp.status_code == 401:
            return "Auth failed (401). Check AUDIPLEX_TOKEN."
        resp.raise_for_status()
    rows = resp.json()
    if command_id:
        rows = [r for r in rows if r.get("id") == command_id]
        if not rows:
            return (
                f"No command #{command_id} on the server. Either it was never "
                f"queued, or it has aged out of the registry."
            )
    if not rows:
        return "No commands have been issued since the server started."

    lines = []
    for r in rows:
        line = f"#{r.get('id')} {r.get('type')}: {r.get('status')}"
        if r.get("delivery_count", 0) > 1:
            line += f" (delivered {r['delivery_count']}x)"
        if r.get("ack_status") and r.get("ack_status") != "ok":
            line += f" — device said '{r['ack_status']}'"
            if r.get("ack_detail"):
                line += f": {r['ack_detail']}"
        elif r.get("status") == "delivered":
            line += " — handed over, no ack yet"
        lines.append(line)
    lines += await _player_error_lines()  # #3249
    return "\n".join(lines)


PLAYER_ERROR_WINDOW_S = 900  # #3249


async def _player_error_lines(window_s: float = PLAYER_ERROR_WINDOW_S) -> list[str]:  # #3249
    """Recent phone player_error entries, loudly worded, newest last.

    An acked command only means the phone TOOK it. 2026-09-28 13:17 play_now
    acked, then the stream 404'd and nothing played; the DJ said "it accepted".
    """
    import time
    try:
        entries = await _get("/api/playback/client-log?limit=50")
    except Exception:
        return []
    cutoff = time.time() - window_s
    out = []
    for e in entries or []:
        when = e.get("received_at") or e.get("at") or 0  # server clock first
        if e.get("event") != "player_error" or when < cutoff:
            continue
        d = e.get("detail") or {}
        mins = max(0, int((time.time() - when) // 60))
        out.append(
            f"PHONE COULD NOT PLAY track {d.get('trackId', '?')} "
            f"({d.get('trackTitle', '?')}): {d.get('causeMessage') or e.get('message')} "
            f"[{mins} min ago]. An ack does not mean it played."
        )
    return out


async def _missing_on_phone(window_s: float = PLAYER_ERROR_WINDOW_S) -> str:  # #ride0928
    """'missing_on_phone: ...' from the newest recent player_error, else ''.

    A phone with the #ride0928 build skips past a file it can't play and says
    skipped=true; an older build just stops on it.
    """
    import time
    try:
        entries = await _get("/api/playback/client-log?limit=50")
    except Exception:
        return ""
    cutoff = time.time() - window_s
    errs = [e for e in entries or []
            if e.get("event") == "player_error" and (e.get("received_at") or e.get("at") or 0) >= cutoff]
    if not errs:
        return ""
    d = errs[-1].get("detail") or {}
    skipped = str(d.get("skipped")).lower() == "true"
    return (f"missing_on_phone: track {d.get('trackId', '?')} ({d.get('trackTitle', '?')}) "
            f"skipped={'yes' if skipped else 'no'}"
            + ("" if skipped else " - the player may be stuck on it; dj_skip moves on."))


@mcp.tool()
async def dj_link_history(limit: int = 25) -> str:
    """When the DJ link to the phone dropped, and when it came back.

    Durable across server restarts, unlike dj_device_status, which only ever
    knows about right now. This exists because on 2026-08-18 nobody could
    answer "did the link actually hold overnight?" — the only record was a
    single in-memory timestamp, so a continuous beat and a night full of gaps
    looked identical.

    A 'resumed (after server restart)' line means WE restarted; it is not a
    device fault and is deliberately not counted as a gap.
    """
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{AUDIPLEX_URL}/api/playback/link-history?limit={max(limit, 1)}",
            headers=_headers(),
        )
        if resp.status_code == 401:
            return "Auth failed (401). Check AUDIPLEX_TOKEN."
        resp.raise_for_status()
    rows = resp.json()
    if not rows:
        return (
            "No link history recorded yet. Expect the first entry as soon as a "
            "player polls; gaps are only written when one actually happens."
        )
    lines = []
    for r in rows:
        if r.get("event") == "resumed":
            when = _fmt_time(r.get("at"))
            suffix = " (after server restart)" if r.get("after_restart") else ""
            lines.append(f"{when}: link resumed{suffix}")
        else:
            lines.append(
                f"{_fmt_time(r.get('from'))} → {_fmt_time(r.get('to'))}: "
                f"GAP of {_fmt_duration(r.get('seconds', 0))}"
            )
    return "\n".join(lines)


@mcp.tool()
async def dj_skip() -> str:
    """Skip to the next track in the Audiplex device's current queue.

    No-op if nothing is queued after the current track. Use dj_now_playing
    afterward to confirm what's playing.
    """
    data = await _enqueue("skip", {})  # #ride0928: one send path for every command
    return await _acked(data, "skip")  # #3505


# --- #3249: announce before music starts, and never send a dead file -------
#
# Todd's rule (2026-09-28 13:20): a persona ANNOUNCES before music starts, so he
# can pause his audiobook first. Any command that could start playback on an
# idle player is refused unless a persona said something about the music in
# Pantheon chat in the last ANNOUNCE_WINDOW_S. The refusal says what to say.

START_CMDS = {"play_now", "resume", "play_stream", "queue", "play_next"}  # #3249
ANNOUNCE_WINDOW_S = 180  # #3249
_PERSONA_SENDERS = ("claude", "gemini", "orolo", "bosley")  # #3249
_MUSIC_WORDS = re.compile(  # #3249
    r"\b(music|tunes?|songs?|tracks?|mix|set|first up|audiplex|dj|playlist|album|"
    r"pause your|playing|starting)\b",
    re.IGNORECASE,
)
ANNOUNCE_REFUSAL = (  # #3249
    "REFUSED (announce first): Todd's rule is that a persona announces before any "
    "music starts, so he can pause his audiobook or YouTube. Nothing was sent. Do "
    "this now: say() one line such as \"Starting the music in ten seconds, Boss: "
    "first up <title> by <artist>. Pause your book.\" Wait about ten seconds, then "
    "call this tool again. Keep it to ten seconds; he notices when it runs long."
)


def _pantheon_db_path() -> Path:  # #3249
    return Path(os.environ.get("DJ_PANTHEON_DB") or "Q:/Pantheon/data/pantheon.db")


def _recent_announcement(now: "datetime.datetime | None" = None) -> bool | None:  # #3249
    """True if a persona mentioned the music in chat recently; None if unreadable."""
    path = _pantheon_db_path()
    if not path.exists():
        return None
    now = now or datetime.datetime.now(datetime.timezone.utc)
    cutoff = (now - datetime.timedelta(seconds=ANNOUNCE_WINDOW_S)).strftime("%Y-%m-%dT%H:%M:%S")
    try:
        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5)
        try:
            rows = con.execute(
                "SELECT content FROM messages WHERE timestamp >= ? AND sender IN "
                f"({','.join('?' * len(_PERSONA_SENDERS))})",
                (cutoff, *_PERSONA_SENDERS),
            ).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    return any(_MUSIC_WORDS.search(r[0] or "") for r in rows)


async def _announce_gate(cmd_type: str) -> str | None:  # #3249
    """The refusal text if this command would start music unannounced, else None."""
    if cmd_type not in START_CMDS:
        return None
    try:
        state = await _get("/api/playback/state")
    except Exception:
        state = {}
    if state.get("playing"):
        return None  # already playing: nothing new starts
    if cmd_type in ("queue", "play_next") and state.get("queue"):
        return None  # appending to a loaded (paused) queue doesn't start it
    announced = _recent_announcement()
    if announced is None:
        return None  # can't read chat: don't block music on our own blindness
    return None if announced else ANNOUNCE_REFUSAL


async def _playable_split(track_ids: list[int]) -> tuple[list[int], list[int]]:  # #3249
    """(playable, missing). A server without /tracks/playable passes all through."""
    # Fail-open on purpose: a server not yet restarted onto /tracks/playable must
    # not stop all music. The stream still 404s a dead file; this is the guard.
    try:
        verdict = await _post("/api/playback/tracks/playable", {"track_ids": track_ids})
    except Exception:
        return list(track_ids), []
    return list(verdict.get("playable") or []), list(verdict.get("missing") or [])


def _missing_note(data) -> str:  # #3249
    n = len(data.get("dropped_missing") or []) if isinstance(data, dict) else 0
    if not n:
        return ""
    return (f" Skipped {n} track(s) whose file isn't on disk (they would 404 on the "
            "phone and play nothing).")


def _sent_count(data, asked: int) -> int:  # #3249: count what was SENT, not asked
    return int(data.get("sent_count", asked)) if isinstance(data, dict) else asked


async def _titles(ids: list[int], cap: int = 10) -> list[str]:  # #ride0928
    """'Title - Artist' for up to `cap` ids (a dead file still has its DB row)."""
    out = []
    for i in ids[:cap]:
        try:
            t = await _get(f"/api/music/tracks/{i}")
            out.append(f"{t.get('title') or '?'} - {t.get('artist_name') or '?'}".strip(" -"))
        except Exception:
            out.append(f"track {i}")
    if len(ids) > cap:
        out.append(f"+{len(ids) - cap} more")
    return out


ACK_WAIT_S = 8.0  # #ride0928


def _held_kind(text: str) -> str:  # #ride0928
    if text.startswith("HELD (todd_talking)"):
        return "todd_talking"
    if text.startswith("REFUSED (muted)"):  # #3493
        return "muted"
    if text.startswith("REFUSED (announce first)"):
        return "announce_first"
    if text.startswith("STOPPED"):  # #3505
        return "stopped"
    return "not_sent"


def _held_result(text: str) -> str:  # #ride0928: same shape as _result, for a refusal
    return f"RESULT sent=0 held={_held_kind(text)} skipped_missing=[] phone_ack=none\n{text}"


async def _ack_row(command_id: int, timeout: float) -> dict | None:  # #3505
    """The registry row once the device has answered `command_id`, else None."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        try:
            for row in await _get("/api/playback/commands?limit=50") or []:
                if isinstance(row, dict) and row.get("id") == command_id and row.get("ack_status"):
                    return row
        except Exception:
            pass
        if loop.time() >= deadline:
            return None
        await asyncio.sleep(min(0.25, max(0.01, deadline - loop.time())))


async def _acked(data, did: str) -> str:  # #3505 (Karen rider b)
    """Honest result for a command that doesn't start audio: DONE only when the
    device acked ok. A refusal, a failed ack or silence all say NOT DONE, so a
    persona can never read "queued" as "it happened"."""
    if isinstance(data, str):
        return f"NOT DONE: {did} was not sent. {data}"
    cid = data.get("id")
    ack = await _ack_row(int(cid), ACK_WAIT_S) if cid is not None else None
    if ack is None:
        return (f"NOT DONE (unconfirmed): {did} was sent (command #{cid}) but the device "
                f"did not answer in {ACK_WAIT_S:g}s. Do NOT tell Todd it happened; check "
                f"dj_command_status({cid}) or dj_now_playing.")
    if ack.get("ack_status") != "ok":
        detail = f": {ack['ack_detail']}" if ack.get("ack_detail") else ""
        hint = (" The phone's Audiplex app is too old for this command; Todd needs to "
                "install the current build." if ack.get("ack_status") == "unknown_type" else "")
        return (f"NOT DONE: {did} - the device answered {ack.get('ack_status')}{detail} "
                f"(command #{cid}).{hint} Do NOT tell Todd it happened.")
    return f"DONE: {did} (command #{cid}, device acked ok)."


# #2843: "the DJ says your phone took the play command, but no music plays."
# An ack is not a sound: an old phone build acks on receipt, and the new one's
# honest "failed: not playing 8s after load" lands AFTER the old 8 s MCP wait.
# So a start is only YES when the device acked ok AND then reported playing the
# thing we asked for; a failed ack is NO; anything else is UNCONFIRMED.
START_VERIFY_S = 15.0  # #2843: total cap; phone START_TIMEOUT 8 s + poll latency
VERIFY_POLL_S = 1.0  # #2843
ALREADY_PLAYING_FRESH_S = 90.0  # #2843: queue/play_next on a live player
VERIFY_CMDS = {"play_now", "resume", "play_stream", "play_book", "queue", "play_next"}  # #2843
NOT_YES_TEXT = {  # #2843: what the persona must (not) say
    "NO": "DID NOT START. Do NOT tell Todd it's playing; tell him it didn't start and why.",
    "UNCONFIRMED": ("NOT CONFIRMED. Do NOT tell Todd it's playing; say you sent it but "
                    "can't confirm it started (check dj_now_playing)."),
}


def _state_says(state, cmd: str, payload: dict) -> str | None:  # #2843
    """'match' if `state` shows the asked-for thing playing, 'idle' if it shows
    nothing playing, 'other: <title>' if something else plays, None if unreadable."""
    if not isinstance(state, dict):
        return None
    track, book = state.get("track"), state.get("book")
    if not state.get("playing"):
        return "idle"
    title = (track or {}).get("title") or (book or {}).get("title") or "?"
    if cmd == "play_now":
        ok = isinstance(track, dict) and track.get("id") in (payload.get("track_ids") or [])
    elif cmd == "play_stream":  # a stream item is id -1 on both renderers
        ok = isinstance(track, dict) and (track.get("id") or 0) < 0 and (
            not payload.get("title") or track.get("title") in (None, payload.get("title")))
    elif cmd == "play_book":
        ok = isinstance(book, dict) and book.get("id") == payload.get("book_id")
    else:  # resume / queue / play_next / activate: anything audible counts
        ok = bool(track or book)
    return "match" if ok else f"other: {title}"


async def _verify_start(command_id: int | None, cmd: str, payload: dict,
                        state_path: str = "/api/playback/state",
                        since: float | None = None) -> tuple[str, str, dict | None]:  # #2843
    """(YES|NO|UNCONFIRMED, why, ack row). Returns the moment the verdict is
    known; never waits longer than START_VERIFY_S in total."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + START_VERIFY_S
    ack: dict | None = None
    says, fresh = None, False
    while True:
        if command_id is not None and ack is None:
            try:
                for row in await _get("/api/playback/commands?limit=50") or []:
                    if isinstance(row, dict) and row.get("id") == command_id:
                        since = row.get("created_at") or since
                        if row.get("ack_status"):
                            ack = row
            except Exception:
                pass
        if ack is not None and ack.get("ack_status") != "ok":
            detail = f": {ack['ack_detail']}" if ack.get("ack_detail") else ""
            return "NO", f"the device answered {ack.get('ack_status')}{detail}", ack
        try:
            state = await _get(state_path)
        except Exception:
            state = None
        says = _state_says(state, cmd, payload)
        at = state.get("updated_at") if isinstance(state, dict) else None
        age = (time.time() - float(at)) if at else None
        fresh = bool(at) and since is not None and float(at) >= float(since)
        acked_ok = ack is not None or command_id is None
        if acked_ok and says == "match" and (
                fresh or (cmd in ("queue", "play_next") and age is not None
                          and age < ALREADY_PLAYING_FRESH_S)):
            return "YES", "the device reports it playing", ack
        if loop.time() >= deadline:
            break
        await asyncio.sleep(VERIFY_POLL_S)
    if command_id is not None and ack is None:
        return "UNCONFIRMED", f"no answer from the device in {START_VERIFY_S:g}s", None
    if fresh and says == "idle":
        return "NO", f"the device reports it is NOT playing {START_VERIFY_S:g}s later", ack
    if fresh and says and says.startswith("other"):
        return "NO", f"the device is playing something else ({says[7:]})", ack
    return ("UNCONFIRMED", "the device accepted it but never reported playing it "
            "(an older phone build acks on receipt, not on sound)", ack)


def _is_yes(result: str) -> bool:  # #2843: may a tool's prose say it's playing?
    return " playing=YES" in result.split("\n", 1)[0]


async def _result(data: dict, asked: int, wait_ack: bool = True) -> str:  # #ride0928
    """One machine-readable line every start-type tool leads with:
    sent / held / skipped_missing (titles) / phone_ack (status: detail) /
    playing (#2843: YES only when the device reported it playing)."""
    sent = _sent_count(data, asked)
    titles = await _titles(list(data.get("dropped_missing") or []))
    cmd = data.get("_cmd") or data.get("type")  # #2843
    if wait_ack and cmd in VERIFY_CMDS and data.get("id") is not None:
        verdict, why, ack = await _verify_start(int(data["id"]), cmd, data.get("_payload") or {})
        phone = "none" if ack is None else str(ack.get("ack_status")) + (
            f": {ack['ack_detail']}" if ack.get("ack_detail") else "")
        line = (f"RESULT sent={sent} held=no skipped_missing={json.dumps(titles, ensure_ascii=False)} "
                f"phone_ack={phone} playing={verdict}\n")
        if verdict != "YES":
            line += f"PLAYING={verdict}: {why}. {NOT_YES_TEXT[verdict]}\n"
        return line
    phone = "not_waited"
    if wait_ack and data.get("id") is not None:
        ack = await _await_ack(int(data["id"]), timeout=ACK_WAIT_S)
        if ack is None:
            phone = f"none_yet (no answer in {ACK_WAIT_S:g}s; see dj_command_status({data['id']}))"
        else:
            phone = str(ack.get("ack_status"))
            if ack.get("ack_detail"):
                phone += f": {ack['ack_detail']}"
    return (f"RESULT sent={sent} held=no skipped_missing={json.dumps(titles, ensure_ascii=False)} "
            f"phone_ack={phone}\n")


# The phone resolves a queued list with one GET per track, in sequence, and its
# command loop stops polling meanwhile: a 990-track queue went dark ~3 min on
# 2026-09-28 (13:17 and 13:37) and read as "no player connected". Big lists go
# out as several small commands so the first lands fast and polling continues.
QUEUE_CHUNK = 40  # #3249
_CHUNKABLE = {"play_now", "queue"}  # #3249


TALK_GUARDED = START_CMDS | {"activate", "play_book"}  # #ride0928 (+ #2680): anything that can start audio on an idle player
TALK_HELD = (  # #ride0928
    "HELD (todd_talking): Todd is talking or typing right now, so nothing was sent. "
    "Music never starts over him. Wait until he's done, then call this again."
)
TALK_UNREADABLE = (  # #ride0928
    "HELD (todd_talking): Pantheon's speech state exists but can't be read, so it "
    "isn't safe to assume Todd is quiet. Nothing was sent. Try again in a moment."
)


MUTE_GUARDED = TALK_GUARDED | {"bed_play", "announce"}  # #3493: muted means nothing audible
REFILL_CMDS = {"queue", "play_next"}  # #3493
DEFAULT_PANTHEON_SRC = "Q:/Pantheon/src"  # #3493


def _pantheon_src() -> Path:  # #3493
    return Path(os.environ.get("DJ_PANTHEON_SRC") or DEFAULT_PANTHEON_SRC)


def _global_mute() -> tuple[bool, str] | None:  # #3493
    """(muted, why) from Pantheon's quiet window / phone DND, or None when there
    is no Pantheon on this box. The GLOBAL mutes only: a muted desk speaker
    never blocks the phone (#1729). Anything but a missing directory that goes
    wrong reads as muted (fail closed)."""
    src = _pantheon_src()
    if not src.is_dir():
        return None
    try:
        if str(src) not in sys.path:
            sys.path.append(str(src))  # append: Pantheon names must not shadow ours
        from device_mute_tell import global_mute_active
        return global_mute_active(fail_closed=True)
    except Exception as e:
        return True, f"mute read failed ({type(e).__name__}: {e}) - failing closed"


def _mute_hold(cmd_type: str) -> str | None:  # #3493 (#5971 contract, event 24162)
    """The refusal text if `cmd_type` would make sound under quiet time/DND, else None.
    Stop-type commands (pause, skip, volume...) are never gated: music already
    playing is never stopped by this."""
    if cmd_type not in MUTE_GUARDED:
        return None
    verdict = _global_mute()
    if verdict is None:
        print(f"[mute guard] no Pantheon at {_pantheon_src()}; {cmd_type} sent unguarded",
              file=sys.stderr, flush=True)
        return None
    muted, why = verdict
    if not muted:
        return None
    text = (f"REFUSED (muted): Nothing was sent: Todd is in quiet time/DND ({why}). "
            "If he asked for this, offer to end quiet time, then retry. "
            "Do not tell him it is playing.")
    if cmd_type in REFILL_CMDS:  # Karen rider (a): a running set is no longer fed
        text += (" Any DJ set already playing was NOT refilled: it will stop when its "
                 "current queue runs out. Tell Todd that; do not assume it keeps going.")
    return text


def _talk_hold(cmd_type: str) -> str | None:  # #ride0928
    """The hold text if `cmd_type` would put audio on while Todd talks (or is
    muted, #3493), else None."""
    muted = _mute_hold(cmd_type)  # #3493: checked first, sends nothing
    if muted:
        return muted
    if cmd_type not in TALK_GUARDED:
        return None
    verdict = _todd_talking()
    if verdict == "talking":
        return TALK_HELD
    if verdict == "unreadable":
        return TALK_UNREADABLE
    if verdict == "missing":
        print(f"[talk guard] no speech state at {_speech_state_path()}; {cmd_type} sent unguarded",
              file=sys.stderr, flush=True)
    return None


async def _enqueue(cmd_type: str, payload: dict) -> str:
    held = _talk_hold(cmd_type)  # #ride0928: before anything else, incl. the announce gate
    if held:
        return held
    refusal = await _announce_gate(cmd_type)  # #3249
    if refusal:
        return refusal
    dropped: list[int] = []  # #3249
    kept: list[int] = []
    if payload.get("track_ids"):
        kept, dropped = await _playable_split(payload["track_ids"])
        if not kept:
            return (f"None of those {len(dropped)} track(s) has a file on disk, so "
                    "nothing was sent: it would ack and play silence. (#3249)")
        payload = {**payload, "track_ids": kept}
    rest: list[int] = []  # #3249
    if cmd_type in _CHUNKABLE and len(kept) > QUEUE_CHUNK:
        payload = {**payload, "track_ids": kept[:QUEUE_CHUNK]}
        rest = kept[QUEUE_CHUNK:]
    data = await _enqueue_raw(cmd_type, payload)
    if not isinstance(data, dict):
        return data
    chunks, sent = 1, len(payload.get("track_ids") or [])
    for i in range(0, len(rest), QUEUE_CHUNK):  # #3249: FIFO bus keeps order
        piece = rest[i:i + QUEUE_CHUNK]
        more = await _enqueue_raw("queue", {"track_ids": piece})
        if not isinstance(more, dict):
            break
        chunks, sent = chunks + 1, sent + len(piece)
    if kept:
        data["sent_count"], data["chunks"] = sent, chunks
    data["_cmd"] = cmd_type  # #2843: _result verifies against what was asked
    data["_payload"] = {**payload, "track_ids": kept} if kept else payload
    if dropped:
        data["dropped_missing"] = dropped
    return data


async def _device_lacks_replace_upcoming() -> bool:  # #3249
    """True if the phone's latest answer to replace_upcoming was unknown_type.

    Read from the server's command history, so every persona's MCP process
    learns it from one failure instead of each re-sending it (13:37:21 failed,
    13:37:32 was sent again).
    """
    try:
        rows = await _get("/api/playback/commands?limit=50")
    except Exception:
        return False
    latest = [r for r in rows or [] if r.get("type") == "replace_upcoming" and r.get("ack_status")]
    return bool(latest) and latest[-1].get("ack_status") == "unknown_type"


async def _enqueue_raw(cmd_type: str, payload: dict) -> str:  # #3249: pre-gate send
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{AUDIPLEX_URL}/api/playback/command",
            headers=_headers(),
            json={"type": cmd_type, "payload": payload},
        )
    if resp.status_code == 401:
        return "Auth failed (401). Check AUDIPLEX_TOKEN."
    if resp.status_code == 409:  # #3505: the stop latch refused a refill
        try:
            return resp.json().get("detail") or "STOPPED: the server refused it (409)."
        except ValueError:
            return "STOPPED: the server refused it (409)."
    resp.raise_for_status()
    return resp.json()


@mcp.tool()
async def dj_queue(track_ids: list[int]) -> str:
    """Append the given music tracks to the END of the device's current queue.

    If nothing is currently playing, this starts playback (same as dj_play_now).
    track_ids are Audiplex music track IDs, played in the order given.
    """
    if not track_ids:
        return "No track_ids given; nothing to queue."
    data = await _enqueue("queue", {"track_ids": track_ids})
    if isinstance(data, str):
        return _held_result(data)  # #ride0928
    return await _result(data, len(track_ids)) + (  # #ride0928
        f"Queued {_sent_count(data, len(track_ids))} track(s) to the end "
        f"(command #{data.get('id')}, {data.get('pending')} pending). "
        "Appended when the device next polls."
    ) + _missing_note(data) + await _repeat_note(track_ids)  # #3249


@mcp.tool()
async def dj_play_next(track_ids: list[int]) -> str:
    """Insert the given music tracks immediately AFTER the currently-playing
    track, so they play next without disturbing the rest of the queue.

    If nothing is currently playing, this starts playback (same as dj_play_now).
    """
    if not track_ids:
        return "No track_ids given; nothing to insert."
    data = await _enqueue("play_next", {"track_ids": track_ids})
    if isinstance(data, str):
        return _held_result(data)  # #ride0928
    return await _result(data, len(track_ids)) + (  # #ride0928
        f"Inserted {_sent_count(data, len(track_ids))} track(s) to play next "
        f"(command #{data.get('id')}, {data.get('pending')} pending)."
    ) + _missing_note(data) + await _repeat_note(track_ids)  # #3249


@mcp.tool()
async def dj_reorder(from_index: int, to_index: int) -> str:
    """Move a queued track from one position to another. Indices are 0-based
    positions in the current queue — read them from dj_now_playing, which lists
    the queue with its indices.
    """
    data = await _enqueue("reorder", {"from_index": from_index, "to_index": to_index})
    return await _acked(data, f"reorder {from_index} -> {to_index}")  # #3505


@mcp.tool()
async def dj_pause() -> str:
    """Pause playback on the Audiplex device."""
    data = await _enqueue("pause", {})
    return await _acked(data, "pause")  # #3505


@mcp.tool()
async def dj_resume() -> str:
    """Resume playback on the Audiplex device."""
    data = await _enqueue("resume", {})
    if isinstance(data, str):
        return _held_result(data)  # #ride0928
    return await _result(data, 0) + f"Queued resume (command #{data.get('id')}, {data.get('pending')} pending)."


@mcp.tool()
async def dj_previous() -> str:
    """Go back to the previous track in the Audiplex device's current queue."""
    data = await _enqueue("previous", {})
    return await _acked(data, "previous")  # #3505


@mcp.tool()
async def dj_seek(position_seconds: int) -> str:
    """Seek to an absolute position (in seconds) in the current track."""
    data = await _enqueue("seek", {"position_ms": position_seconds * 1000})
    return await _acked(data, f"seek to {position_seconds}s")  # #3505


@mcp.tool()
async def dj_volume(level: int) -> str:
    """Set the Audiplex app's player volume, 0-100. This is Media3 player
    volume, which multiplies with the phone's device volume — it does NOT
    change the device/stream volume."""
    if not 0 <= level <= 100:
        return f"level must be 0-100 (got {level})."
    data = await _enqueue("volume", {"volume": level / 100.0})
    return await _acked(data, f"volume {level}%")  # #3505


@mcp.tool()
async def dj_play_stream(url: str, title: str = "Live stream") -> str:
    """Play an external HTTP audio stream on the Audiplex device, replacing
    the current queue — this is how agents route Radio Free Luna (or any
    other HTTP audio stream) to the phone, e.g.
    url='http://<rfl-host>:8080/stream.mp3'. Use dj_play_now afterward to
    switch back to music. Play/stop/switch-source only — the queue-ops
    tools (dj_queue/dj_reorder/etc.) don't apply to a stream item.
    """
    data = await _enqueue("play_stream", {"url": url, "title": title})
    if isinstance(data, str):
        return _held_result(data)  # #ride0928
    return await _result(data, 1) + (
        f"Sent stream '{title}' from {url} "  # #2843
        f"(command #{data.get('id')}, {data.get('pending')} pending)."
    )


@mcp.tool()
async def dj_play_book(book: str, position_seconds: float = -1) -> str:
    """Play an audiobook on the active PC renderer (#2680), resuming where Todd
    left off on ANY device (the server's saved position, which the phone also
    reads and writes). `book` is a book id or part of its title.
    position_seconds >= 0 starts there instead. The PC saves its position back
    as it plays, so opening the book on the phone later picks up at the PC's
    spot. The phone app plays books from its own UI, not by this command; use
    dj_transfer to move a playing book between phone and PC. Talk-guarded.
    """
    key = book.strip()
    try:
        books = await _get("/api/library/books")
    except Exception as exc:
        return f"Couldn't list books: {exc!r}"[:300]
    if key.isdigit():
        matches = [b for b in books if b.get("id") == int(key)]
    else:
        low = key.lower()
        matches = [b for b in books if low in str(b.get("title", "")).lower()]
        exact = [b for b in matches if str(b.get("title", "")).lower() == low]
        matches = exact or matches
    if not matches:
        return f"No book matches '{book}'."
    if len(matches) > 1:
        names = "; ".join(f"{b['id']}: {b.get('title')}" for b in matches[:8])
        return f"'{book}' matches {len(matches)} books, pick one by id: {names}"
    target = matches[0]
    payload: dict = {"book_id": target["id"], "playing": True}
    if position_seconds >= 0:
        payload["position_ms"] = int(position_seconds * 1000)
    data = await _enqueue("play_book", payload)
    if isinstance(data, str):
        return _held_result(data)
    return await _result(data, 1) + (
        f"Sent '{target.get('title')}' (book {target['id']}) "
        + ("from the saved position" if position_seconds < 0 else f"from {position_seconds:.0f} s")
        + f" (command #{data.get('id')}). A phone renderer answers unknown_type: books only follow to the PC."
    )


# ----- Sleep engine (#1728): a second, independent looping layer plus a -----
# ----- fade-out timer on the main player.                               -----
#
# The "bed" is a SEPARATE player on the device (not the queue/session the
# tools above control) so it can loop forever underneath whatever else is
# playing, with its own volume. The "fade" layer is just the EXISTING main
# player (dj_play_now/dj_play_stream) plus a timer that ramps dj_volume to 0
# and pauses it — the bed keeps going. Two fixed layers, matching the actual
# want (a drone bed + one timed thing on top), not a general N-layer mixer.


@mcp.tool()
async def dj_bed_play(url: str, title: str = "Sleep bed", volume: int = 50) -> str:
    """Start (or replace) the continuously-LOOPING sleep-bed layer — a second,
    independent audio stream that plays underneath normal playback and repeats
    forever until dj_bed_stop is called. Does not touch or interrupt the main
    player (audiobook/music/stream) — the two run at once with independent
    volume. url is any HTTP audio URL the device can reach (a catalog stream
    URL, or an external one like Radio Free Luna). volume is 0-100,
    independent of dj_volume (which only affects the main player).
    """
    if not 0 <= volume <= 100:
        return f"volume must be 0-100 (got {volume})."
    data = await _enqueue("bed_play", {"url": url, "title": title, "volume": volume / 100.0})
    return await _acked(data, f"sleep-bed loop '{title}' from {url} at {volume}%")  # #3505


@mcp.tool()
async def dj_bed_stop() -> str:
    """Stop the sleep-bed loop layer started by dj_bed_play. Leaves the main
    player (audiobook/music/stream) untouched."""
    data = await _enqueue("bed_stop", {})
    return await _acked(data, "sleep-bed stop")  # #3505


@mcp.tool()
async def dj_bed_volume(level: int) -> str:
    """Set the sleep-bed loop layer's volume, 0-100 — independent of
    dj_volume, which only affects the main player."""
    if not 0 <= level <= 100:
        return f"level must be 0-100 (got {level})."
    data = await _enqueue("bed_volume", {"volume": level / 100.0})
    return await _acked(data, f"sleep-bed volume {level}%")  # #3505


@mcp.tool()
async def dj_sleep_timer(minutes: float, fade_seconds: int = 120, bed_fade_to: int | None = None) -> str:
    """Fade out and pause the MAIN player (music, audiobook or stream: whatever
    dj_play_now/dj_play_book/dj_play_stream started) after `minutes`, ramping
    its volume to 0 over the last `fade_seconds`. The sleep-bed loop
    (dj_bed_play) keeps playing underneath, unless bed_fade_to (0-100) is
    given: then the bed ramps UP to that volume over the same window (#3367).

    #3505: this is checked, not fire-and-forget. If the phone's app doesn't
    know sleep_timer (builds before 2026-09-07 answer unknown_type) or doesn't
    answer, the SERVER runs the fade itself (volume steps, then pause, then it
    verifies playing=no); bed_fade_to is not available on that path. Read the
    first line: it says which path is armed. dj_stop_status reports the
    server path's verdict. To stop DJ music at the end of the current song,
    use dj_stop_after_current instead. Cancel with dj_cancel_sleep_timer.
    """
    if minutes <= 0:
        return "minutes must be > 0."
    payload = {"minutes": minutes, "fade_seconds": fade_seconds}
    if bed_fade_to is not None:
        if not 0 <= bed_fade_to <= 100:
            return f"bed_fade_to must be 0-100 (got {bed_fade_to})."
        payload["bed_fade_to"] = bed_fade_to / 100.0
    data = await _enqueue("sleep_timer", payload)
    if isinstance(data, str):
        return f"NOT ARMED: {data}"
    cid = data.get("id")
    ack = await _ack_row(int(cid), ACK_WAIT_S) if cid is not None else None
    if ack is not None and ack.get("ack_status") == "ok":
        into = f", crossfading the bed up to {bed_fade_to}%" if bed_fade_to is not None else ""
        return (f"ARMED on the phone (command #{cid} acked ok): fade out over the last "
                f"{fade_seconds}s of {minutes} min{into}.")
    why = ("no answer in " + f"{ACK_WAIT_S:g}s" if ack is None else
           f"the phone answered {ack.get('ack_status')}"
           + (f": {ack['ack_detail']}" if ack.get("ack_detail") else ""))
    try:
        armed = await _post("/api/playback/scheduled-stop",
                            {"mode": "fade", "minutes": minutes, "fade_seconds": fade_seconds})
    except Exception as e:
        return (f"NOT ARMED: the phone did not take the sleep timer ({why}) and the server "
                f"fallback failed too ({e}). Nothing will stop the music; use dj_pause or "
                "dj_stop_after_current, and tell Todd.")
    old = (" The phone's Audiplex app is too old for sleep_timer; Todd should install "
           "the current build." if ack is not None and ack.get("ack_status") == "unknown_type" else "")
    lost = " bed_fade_to is ignored on this path." if bed_fade_to is not None else ""
    return (f"ARMED on the SERVER (fallback): the phone did not take the sleep timer ({why}).{old} "
            f"The server will fade the main player over the last {fade_seconds}s of {minutes} min, "
            f"pause it and verify it stopped.{lost} Check dj_stop_status afterwards; "
            f"until it says verdict YES, do not tell Todd the music stopped. (job: {armed.get('job')})")


@mcp.tool()
async def dj_cancel_sleep_timer() -> str:
    """Cancel a pending dj_sleep_timer fade-out on the phone AND any server-run
    fallback (#3505), restoring the configured volume. Does not affect the
    sleep-bed loop. Also lifts a stop latch."""
    data = await _enqueue("cancel_sleep_timer", {})
    phone = await _acked(data, "phone sleep-timer cancel")
    try:
        cleared = await _delete("/api/playback/scheduled-stop")
        server = "Server stop cleared." if cleared.get("cleared") else "No server stop was armed."
    except Exception as e:
        server = f"Server stop NOT cleared: {e}"
    return f"{phone}\n{server}"


# ----- #3505: stop after the current song, verified -----


def _describe_stop(stop: dict | None) -> str:
    if not isinstance(stop, dict) or not (stop.get("job") or stop.get("latched")):
        return "No DJ stop is armed and nothing is latched."
    lines = []
    job = stop.get("job") or {}
    if job:
        state = "ARMED" if stop.get("active") else "FINISHED"
        verdict = job.get("verdict")
        head = f"{state} {job.get('mode')} stop"
        if job.get("track_title"):
            head += f" after '{job['track_title']}'"
        if job.get("ends_in_s") is not None:
            head += f", song ends in ~{int(job['ends_in_s'])}s"
        if job.get("fires_in_s") is not None:
            head += f", fade finishes in ~{int(job['fires_in_s'])}s"
        if job.get("trim"):
            head += f" (queue trim: {job['trim']})"
        lines.append(head + ".")
        if verdict == "YES":
            lines.append(f"VERIFIED stopped: {job.get('reason')}.")
        elif verdict == "NO":
            lines.append(f"NOT VERIFIED - the music may still be playing: {job.get('reason')}. "
                         "Tell Todd plainly; try dj_pause.")
        elif verdict == "CANCELLED":
            lines.append(f"Cancelled: {job.get('reason')}.")
        else:
            lines.append("Not done yet: NOT verified. Do not tell Todd it stopped until "
                         "dj_stop_status says VERIFIED.")
    if stop.get("latched"):
        mins = int((stop.get("latch_expires_in_s") or 0) // 60)
        lines.append(f"STOP LATCH ON ({stop.get('latch_reason')}): dj_queue/dj_play_next are "
                     f"refused for ~{mins} more min. This is deliberate, not a broken queue. "
                     "If Todd asks for music, dj_play_now lifts it (or dj_stop_cancel).")
    return "\n".join(lines)


@mcp.tool()
async def dj_stop_after_current() -> str:
    """Stop the DJ music at the END of the song playing now, and prove it.

    The server trims the rest of the queue, stops the DJ pool, latches against
    refills (dj_queue/dj_play_next are refused until dj_play_now,
    dj_resume or dj_stop_cancel, or 3 h pass), and makes sure the phone
    stops when the song ends: if the phone can't trim (old app build), it
    pauses ~1 s before the end. It then waits for the phone to report
    playing=no. Returns right away with the ETA; call dj_stop_status after the
    song ends for the verdict. Works on every phone build."""
    try:
        armed = await _post("/api/playback/scheduled-stop", {"mode": "after_current"})
    except httpx.HTTPStatusError as e:
        try:
            detail = e.response.json().get("detail")
        except ValueError:
            detail = e.response.text
        return f"NOT ARMED: {detail}"
    except Exception as e:
        return f"NOT ARMED: {e}"
    return "ARMED (not yet verified).\n" + _describe_stop(armed)


@mcp.tool()
async def dj_stop_status() -> str:
    """The verdict of the latest dj_stop_after_current / server sleep-timer
    stop, and whether the stop latch is on. VERIFIED means the phone reported
    playing=no after the stop; anything else is NOT verified."""
    try:
        return _describe_stop(await _get("/api/playback/scheduled-stop"))
    except Exception as e:
        return f"Could not read the stop status: {e}. NOT verified."


@mcp.tool()
async def dj_stop_cancel() -> str:
    """Cancel an armed dj_stop_after_current / server sleep-timer stop and lift
    the stop latch, so dj_queue/dj_play_next work again."""
    try:
        cleared = await _delete("/api/playback/scheduled-stop")
    except Exception as e:
        return f"NOT cancelled: {e}"
    return ("Stop cancelled and latch lifted." if cleared.get("cleared")
            else "Nothing was armed or latched.")


# #3367: the default bed, found by title in the library (any category) so
# dropping Todd's own track into a library root and rescanning switches it over
# with no config edit. First match wins; the generated loop is the fallback.
# AUDIPLEX_SLEEP_BED_URL overrides the lookup outright.
SLEEP_BED_TITLES = (
    "Star Ship Sleeping Quarters",
    "Starship Sleeping Quarters",
    "Sleeping Quarters",
    "Brown Noise - Sleep Loop",
)


async def _default_sleep_bed() -> tuple[str, str] | None:
    """(url, title) of the default sleep bed, or None if none is in the library."""
    env = os.environ.get("AUDIPLEX_SLEEP_BED_URL")
    if env:
        return env, "Sleep bed"
    books = await _get("/api/library/books") or []
    for want in SLEEP_BED_TITLES:
        for b in books:
            if want.lower() in (b.get("title") or "").lower():
                return f"/api/stream/{b['id']}", b["title"]
    return None


@mcp.tool()
async def dj_sleep_start(
    bed_url: str | None = None,
    fade_track_ids: list[int] | None = None,
    fade_stream_url: str | None = None,
    fade_after_minutes: float = 30,
    fade_seconds: int = 120,
    bed_volume: int = 50,
    bed_title: str | None = None,
    bed_mode: str = "crossfade",
) -> str:
    """Sleep mode, in one call. With NO arguments while a book is playing:
    the book keeps playing untouched for 30 min, then fades out over 2 min
    while Todd's brown-noise bed fades in on the same device (phone or PC), and the bed loops
    on all night. That is what "Jarvis, sleep mode" means.

    bed_url: defaults to the brown-noise track found in the library.
    bed_mode: "crossfade" (bed silent until the book fades, then rises) or
    "under" (bed at bed_volume from the start, underneath the book).
    fade_track_ids / fade_stream_url: optionally START something new to fade;
    leave both None to fade whatever is already playing (never restarted or
    re-queued). If nothing is playing and nothing is passed, just the bed
    starts, at bed_volume.
    """
    if bed_mode not in ("crossfade", "under"):
        return f"bed_mode must be 'crossfade' or 'under' (got {bed_mode!r})."
    if not bed_url:
        found = await _default_sleep_bed()
        if not found:
            return ("No sleep bed found: nothing titled like "
                    f"{', '.join(SLEEP_BED_TITLES)} is in the library. Pass bed_url.")
        bed_url, found_title = found
        bed_title = bed_title or found_title
    bed_title = bed_title or "Sleep bed"

    starting = bool(fade_track_ids or fade_stream_url)
    if not starting:
        state = await _get("/api/playback/state") or {}
        # An audiobook reports track=None (it isn't a music track), so a loaded
        # player shows up as playing or as having a duration (#3367).
        fading = bool(state.get("track") or state.get("playing") or state.get("duration_ms"))
    else:
        fading = True
    crossfade = fading and bed_mode == "crossfade"

    notes = [await dj_bed_play(bed_url, bed_title, 0 if crossfade else bed_volume)]
    if fade_track_ids:
        notes.append(await dj_play_now(fade_track_ids))
    elif fade_stream_url:
        notes.append(await dj_play_stream(fade_stream_url))
    if fading:
        notes.append(await dj_sleep_timer(fade_after_minutes, fade_seconds,
                                          bed_volume if crossfade else None))
    else:
        notes.append("Nothing is playing, so there's no timer: just the bed.")
    return "\n".join(notes)


@mcp.tool()
async def dj_break_brief() -> str:
    """Get everything you need to WRITE a DJ voice break: the current daypart's
    persona directive, the local time, optional weather, and what's playing.

    Read-only — this queues nothing. Use it, write 2-4 sentences of on-air copy
    in the register it describes, then pass that copy to dj_announce. Roughly
    one break per 3-5 songs.
    """
    now = datetime.datetime.now()
    part = dj_persona.time_of_day(now)
    persona = dj_persona.DAYPART_PERSONAS[part]

    lines = [
        f"You are {dj_persona.persona_name()}, on air.",
        f"Daypart: {part} ({persona['name']}) — local time "
        f"{now.strftime('%I:%M %p').lstrip('0')}, {now.strftime('%A')}.",
        "",
        f"Directive: {persona['directive']}",
        "",
        f"Style: {dj_persona.STYLE_RULES}",
    ]

    weather = await dj_persona.weather_line()
    if weather:
        lines += ["", f"Outside right now: {weather}."]

    try:
        state = await _get("/api/playback/state")
    except (PermissionError, httpx.HTTPError):
        state = None
    if state and state.get("track"):
        t = state["track"]
        prev, nxt, later = await _prev_next(state)  # #ride0928: same prev/now/next as the bridge watcher
        lines.append("")
        if prev:
            lines.append(f"Previous: {prev.get('title')} - {prev.get('artist')}")
        lines.append(f"Now playing: {t.get('title')} - {t.get('artist')}")
        if nxt:
            lines.append(f"Next: {nxt.get('title')} - {nxt.get('artist')}")
        if later:
            lines.append("After that: " + "; ".join(f"{i.get('title')} - {i.get('artist')}" for i in later))
        lines += await _pair_note_lines(prev, t, nxt)  # #ride0928
    else:
        lines += ["", "Nothing is playing right now."]

    if not tts_backend.is_configured():
        lines += [
            "",
            "WARNING: no TTS backend is configured, so dj_announce will fail. "
            "Set DJ_TTS_URL to an OpenAI-compatible speech endpoint.",
        ]
    return "\n".join(lines)


async def _prev_next(state: dict) -> tuple[dict | None, dict | None, list[dict]]:  # #ride0928
    """(previous, next, the two after next) around the current track.

    Previous = the music item before the current one in the phone's queue,
    else the owner's last play of a different track (/history). DJ voice
    breaks (negative ids) are skipped on both sides.
    """
    queue = [q for q in state.get("queue") or [] if (q.get("id") or 0) > 0]
    idx = state.get("queue_index") or 0
    cur_id = (state.get("track") or {}).get("id")
    before = [q for q in queue if q.get("index", 0) < idx]
    after = [q for q in queue if q.get("index", 0) > idx]
    prev = before[-1] if before else None
    if prev is None:
        try:
            for h in await _get("/api/playback/history?limit=5"):
                if h.get("track_id") != cur_id:
                    prev = {"id": h["track_id"], "title": h.get("title"), "artist": h.get("artist_name")}
                    break
        except Exception:
            pass
    return prev, (after[0] if after else None), after[1:3]


async def _pair_note_lines(prev: dict | None, now: dict | None, nxt: dict | None) -> list[str]:  # #ride0928
    """DJ pair notes for prev->now and now->next (and each track's own notes)."""
    out: list[str] = []
    seen: set[int] = set()
    for a, b in ((prev, now), (now, nxt)):
        if not a or not b or (a.get("id") or 0) <= 0 or (b.get("id") or 0) <= 0:
            continue
        try:
            notes = await _get(f"/api/playback/pair-notes?track_a={a['id']}&track_b={b['id']}&limit=5")
        except Exception:
            continue
        for n in notes:
            if n["id"] in seen:
                continue
            seen.add(n["id"])
            who = f" ({n['persona']})" if n.get("persona") else ""
            about = "this pairing" if n.get("track_b") else f"track {n['track_a']}"
            out.append(f"DJ note on {about}{who}: {n['note']}")
    return ([""] + out) if out else []


@mcp.tool()
async def dj_history(limit: int = 15, since_minutes: float = 0) -> str:
    """What Todd actually PLAYED, newest first (#ride0928): one line per track
    start, from the owner's listening history (not the queue). since_minutes
    limits it to the last N minutes (0 = no limit)."""
    import time
    path = f"/api/playback/history?limit={max(1, min(limit, 200))}"
    if since_minutes > 0:
        path += f"&since={time.time() - since_minutes * 60:.0f}"
    rows = await _get(path)
    if not rows:
        return "No plays on record for that window."
    return "\n".join(
        f"{_fmt_time(r.get('at'))}  {r['track_id']} | {r.get('artist_name') or '?'} - {r.get('title')}"
        for r in rows
    )


@mcp.tool()
async def dj_pair_note(track_a: int, note: str, track_b: int = 0, persona: str = "") -> str:
    """Remember something about a track, or about playing track_a INTO track_b
    (#ride0928). Kept in audiplex.db, so it's there on the next ride, and
    dj_break_brief shows it when that pairing comes up. track_b=0 = a note
    about track_a alone. Examples: "great lift into the climb", "never after
    a ballad", "Todd skips this one past minute 3"."""
    body = {"track_a": track_a, "note": note, "persona": persona or None}
    if track_b:
        body["track_b"] = track_b
    try:
        r = await _post("/api/playback/pair-notes", body)
    except httpx.HTTPStatusError as e:
        return f"Not saved: {e.response.status_code} {e.response.text[:200]}"
    what = f"{track_a} -> {track_b}" if track_b else f"track {track_a}"
    return f"Saved note #{r['id']} on {what}."


@mcp.tool()
async def dj_pair_notes(track_a: int = 0, track_b: int = 0, limit: int = 20) -> str:
    """Read DJ notes (#ride0928). Both ids = that pairing plus each track's own
    notes; track_a alone = every note touching it; neither = the latest."""
    q = [f"limit={max(1, min(limit, 200))}"]
    if track_a:
        q.append(f"track_a={track_a}")
    if track_b:
        q.append(f"track_b={track_b}")
    rows = await _get("/api/playback/pair-notes?" + "&".join(q))
    if not rows:
        return "No DJ notes match."
    return "\n".join(
        f"#{r['id']} {r['track_a']}" + (f" -> {r['track_b']}" if r.get("track_b") else "")
        + f": {r['note']}" + (f" ({r['persona']})" if r.get("persona") else "")
        for r in rows
    )


@mcp.tool()
async def dj_set_kind(kind: str, track_ids: list[int] | None = None, folder: str = "") -> str:
    """Mark tracks as music | podcast | clip | ambient (#ride0928). Only 'music'
    goes into dj_mix and the rolling pool, so a podcast or a rain-sounds bed
    never lands mid-ride. folder = every track under that path."""
    body: dict = {"kind": kind}
    if track_ids:
        body["track_ids"] = track_ids
    if folder:
        body["folder"] = folder
    try:
        r = await _put("/api/playback/content-kind", body)
    except httpx.HTTPStatusError as e:
        return f"Not changed: {e.response.status_code} {e.response.text[:200]}"
    return f"Set {r['updated']} track(s) to {r['kind']}."


@mcp.tool()
async def dj_folder(path: str, action: str = "shuffle", recursive: bool = True, name: str = "") -> str:
    """Do something with a whole folder in one call (#ride0928).

    path:      a folder path as dj_library shows it.
    action:    'shuffle' = a balanced dj_mix of the folder (re-plans what's to
               come, never interrupts the current song); 'queue' = append in
               folder order; 'playlist' = save it as a playlist in Todd's
               library (name defaults to the folder name).
    recursive: include subfolders (default) or only the folder's own files.
    """
    if action == "shuffle":
        return await dj_mix(sources=[{"kind": "folder", "query": path, "recursive": recursive}])
    try:
        label, tracks = await _resolve_source("folder", path, recursive=recursive)
    except (PermissionError, LookupError, httpx.HTTPStatusError) as e:
        return f"Couldn't read folder '{path}': {e}"
    ids = [int(t["id"]) for t in tracks]
    if not ids:
        return f"{label} has no tracks."
    if action == "queue":
        data = await _enqueue("queue", {"track_ids": ids})
        if isinstance(data, str):
            return _held_result(data)
        return await _result(data, len(ids)) + f"Queued {_sent_count(data, len(ids))} track(s) from {label}." + _missing_note(data)
    if action == "playlist":
        title = name or path.replace("\\", "/").rstrip("/").split("/")[-1] or "Folder"
        pl = await _post("/api/playback/playlists", {"name": title, "track_ids": ids})
        return f"Saved playlist '{pl['name']}' (#{pl['id']}) with {pl['track_count']} track(s) from {label}."
    return f"Unknown action '{action}'. Use 'shuffle', 'queue' or 'playlist'."


@mcp.tool()
async def dj_outro(text: str, agent: str = "") -> str:
    """End the ride well (#ride0928, server half #5515): after the CURRENT song
    ends, play your spoken outro, then pause. Never cuts a song. Write the copy
    like a break (no markdown; every character is spoken). Needs music playing.
    Cancel with dj_outro_cancel."""
    text = (text or "").strip()
    if not text:
        return "No text given; nothing armed."
    title = f"Ride outro · {agent}" if agent else "Ride outro"
    clip = await _render_clip(text, title)
    if isinstance(clip, str):
        return clip
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            f"{AUDIPLEX_URL}/api/playback/pool/outro", headers=_headers(),
            json={"clip_id": clip["clip_id"], "duration_seconds": clip.get("duration_seconds"),
                  "title": title, "agent": agent or None, "say": text},
        )
    if resp.status_code == 401:
        return "Auth failed (401). Check AUDIPLEX_TOKEN."
    body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    if resp.status_code >= 400 or not body.get("armed"):
        return f"Outro NOT armed: {body.get('reason') or body.get('detail') or resp.status_code}."
    return (f"Outro armed: plays after the current song (clip #{clip['clip_id']}), then the player "
            "pauses. dj_pool_status shows it; dj_outro_cancel disarms.")


@mcp.tool()
async def dj_outro_cancel() -> str:
    """Disarm a pending ride-end outro (#ride0928). Music keeps going."""
    r = await _delete("/api/playback/pool/outro")
    return "Outro disarmed." if r.get("disarmed") else "No outro was armed."


@mcp.tool()
async def dj_cooldown() -> str:
    """What Todd heard recently enough that a pick would repeat it (#ride0928:
    the /cooldown read had no tool). Same windows dj_check_picks uses."""
    c = await _get("/api/playback/cooldown")
    plays = c.get("recent_plays") or c.get("recent") or []
    head = (f"Cooldown: same recording {c.get('recording_cooldown_minutes', '?')} min, "
            f"same song {c.get('work_cooldown_minutes', '?')} min.")
    if not plays:
        return head + " Nothing inside the window."
    return head + "\n" + "\n".join(
        f"  {p.get('track_id')} | {p.get('artist_name') or ''} - {p.get('title') or ''}".rstrip(" -")
        for p in plays[:30])


async def _put(path: str, body: dict):  # #ride0928
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.put(f"{AUDIPLEX_URL}{path}", headers=_headers(), json=body)
    if resp.status_code == 401:
        raise PermissionError("Auth failed (401). Check AUDIPLEX_TOKEN.")
    resp.raise_for_status()
    return resp.json()


SPEECH_GATE_WAIT_SECONDS = 20  # #2858
DEFAULT_SPEECH_STATE_FILE = "Q:/Pantheon/data/runtime/speech_state.json"  # #ride0928


def _speech_state_path() -> Path:  # #ride0928
    return Path(os.environ.get("DJ_SPEECH_STATE_FILE") or DEFAULT_SPEECH_STATE_FILE)


def _todd_talking(claim: bool = False) -> str:  # #ride0928: one guard for every audio start
    """'talking', 'clear', 'missing' or 'unreadable', from Pantheon's speech state.

    Merges #2858's _someone_talking and #5463's _speech_busy. Any truthy
    stt_active / talk_active / composing is Todd talking or typing; claim=True
    also counts a persona holding the speaking claim (a clip playing).
    'missing' = no Pantheon on this box, so no talk signal exists; 'unreadable'
    = the file is there but broken, which callers treat as talking (fail closed).
    """
    path = _speech_state_path()
    if not path.exists():
        return "missing"
    for attempt in range(2):  # a read can land mid-write; one retry
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            break
        except (OSError, ValueError):
            if attempt:
                return "unreadable"
            import time
            time.sleep(0.05)
    if not isinstance(state, dict):
        return "unreadable"
    keys = ("stt_active", "talk_active", "composing") + (("current_claim_holder",) if claim else ())
    return "talking" if any(state.get(k) for k in keys) else "clear"


def _someone_talking() -> bool:  # #2858
    """True while Todd is talking or typing (or the state can't be read)."""
    return _todd_talking() in ("talking", "unreadable")


async def _render_clip(text: str, title: str) -> dict | str:
    """Synthesize `text` in the DJ voice and upload it to /api/dj/clips.
    The one render path for dj_announce and pre-rendered cue patter (#5480).
    Returns the upload's JSON ({clip_id, url, duration_seconds}) or an error string."""
    try:
        clip_path = await tts_backend.synthesize(text)
    except tts_backend.TtsNotConfigured as e:
        return f"TTS is not configured: {e}"
    except tts_backend.TtsFailed as e:
        return f"Speech synthesis failed: {e}"

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            with open(clip_path, "rb") as fh:
                resp = await client.post(
                    f"{AUDIPLEX_URL}/api/dj/clips",
                    headers=_headers(),
                    files={"file": (clip_path.name, fh, "application/octet-stream")},
                    data={"title": title},
                )
        if resp.status_code == 401:
            return "Auth failed (401) uploading the clip. Check AUDIPLEX_TOKEN."
        if resp.status_code >= 400:
            return f"Clip upload failed ({resp.status_code}): {resp.text[:300]}"
        return resp.json()
    except httpx.HTTPError as e:
        return f"Clip upload failed: {e}"
    finally:
        clip_path.unlink(missing_ok=True)


@mcp.tool()
async def dj_announce(text: str, mode: str = "next", title: str = "DJ break") -> str:
    """Speak a DJ voice break on the device: synthesizes YOUR copy to audio,
    uploads it, and drops it into the queue.

    text:  the on-air copy to speak. Write it yourself with dj_break_brief
           first — every character is synthesized, so no markdown, emoji, or
           bracketed stage directions.
    mode:  'next' (default — plays after the current song finishes, which is
           how a real DJ break lands) or 'now' (interrupt and speak
           immediately).
    title: label shown in the queue (default "DJ break").
    """
    if mode not in ("next", "now"):
        return f"Unknown mode '{mode}'. Use 'next' or 'now'."
    text = (text or "").strip()
    if not text:
        return "No text given; nothing to announce."
    muted = _mute_hold("announce")  # #3493: before the talk wait and the TTS render
    if muted:
        return muted

    # #2858: never start a break while Todd is talking. Checked at queue
    # time only; a 'next' break still plays whenever the current song ends.
    for _ in range(SPEECH_GATE_WAIT_SECONDS):
        if not _someone_talking():
            break
        await asyncio.sleep(1)
    else:
        return "Todd is talking, so no break was queued. Try again in a moment."

    clip = await _render_clip(text, title)
    if isinstance(clip, str):
        return clip

    data = await _enqueue(
        "announce",
        {
            "clip_id": clip["clip_id"],
            "clip_url": clip["url"],
            "title": title,
            "duration_seconds": clip.get("duration_seconds"),
            "mode": mode,
        },
    )
    if isinstance(data, str):
        return data
    secs = clip.get("duration_seconds")
    length = f"{secs:.1f}s" if isinstance(secs, (int, float)) else "unknown length"
    when = "after the current track" if mode == "next" else "immediately"
    return (
        f"Queued a {length} voice break to play {when} "
        f"(clip #{clip['clip_id']}, command #{data.get('id')}, "
        f"{data.get('pending')} pending)."
    )


@mcp.tool()
async def dj_patter(
    on: bool, every_min: int | None = None, every_max: int | None = None
) -> str:
    """Turn DJ patter on or off ("DJ patter on/off"). #2858

    While on, the dj_bridge_watcher invites a persona (Jarvis, Karen, Orolo in
    rotation) to speak a short bridge early in the new song, on every
    every_min-every_max music track changes (default 2-3). Takes effect on
    the watcher's next 2 s tick; nothing is restarted and no music is touched.
    """
    settings = dj_bridge_watcher.load_settings()
    settings["on"] = bool(on)
    if every_min is not None:
        settings["every_min"] = max(1, int(every_min))
    if every_max is not None:
        settings["every_max"] = int(every_max)
    settings["every_max"] = max(settings["every_min"], settings["every_max"])
    dj_bridge_watcher.write_settings(settings)
    state = "on" if settings["on"] else "off"
    return (
        f"DJ patter {state} (a bridge every {settings['every_min']}-"
        f"{settings['every_max']} track changes). The watcher process must be "
        "running (launch-dj-bridge-hidden.vbs)."
    )


async def _get(path: str):
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(f"{AUDIPLEX_URL}{path}", headers=_headers())
    if resp.status_code == 401:
        raise PermissionError("Auth failed (401). Check AUDIPLEX_TOKEN.")
    resp.raise_for_status()
    return resp.json()


async def _post(path: str, body: dict):
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            f"{AUDIPLEX_URL}{path}", headers=_headers(), json=body
        )
    if resp.status_code == 401:
        raise PermissionError("Auth failed (401). Check AUDIPLEX_TOKEN.")
    resp.raise_for_status()
    return resp.json()


async def _delete(path: str):
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.delete(
            f"{AUDIPLEX_URL}{path}", headers=_headers()
        )
    if resp.status_code == 401:
        raise PermissionError("Auth failed (401). Check AUDIPLEX_TOKEN.")
    resp.raise_for_status()
    return resp.json()


async def _delete_json(path: str, body: dict):  # #2806: DELETE with a body
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.request(
            "DELETE", f"{AUDIPLEX_URL}{path}", headers=_headers(), json=body
        )
    if resp.status_code == 401:
        raise PermissionError("Auth failed (401). Check AUDIPLEX_TOKEN.")
    resp.raise_for_status()
    return resp.json()


async def _patch(path: str, body: dict):  # #5495
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.patch(
            f"{AUDIPLEX_URL}{path}", headers=_headers(), json=body
        )
    if resp.status_code == 401:
        raise PermissionError("Auth failed (401). Check AUDIPLEX_TOKEN.")
    resp.raise_for_status()
    return resp.json()

def _describe_suppression(item: dict) -> str:
    label = f"{item.get('artist_name') or ''} - {item.get('title') or ''}".strip(" -")
    return f"    {label or ('track ' + str(item['track_id']))} — {item['detail']}"


async def _repeat_note(track_ids: list[int]) -> str:
    """Advisory line for a queueing command: did this just play? (#948)

    Deliberately does NOT filter. An explicit request is an explicit request —
    when Todd asks for a song by name he gets that song. This only appends a
    note, so a repeat is visible rather than silent, and a failure to reach the
    cooldown read can never stop a command from being sent.
    """
    try:
        verdict = await _post("/api/playback/candidates/filter", {"track_ids": track_ids})
    except Exception:
        return ""
    suppressed = verdict.get("suppressed") or []
    if not suppressed:
        return ""
    lines = [
        "",
        f"  Heads-up: {len(suppressed)} of these played recently (queued anyway):",
    ]
    lines.extend(_describe_suppression(s) for s in suppressed)
    return "\n".join(lines)


def _best_match(items: list[dict], name_key: str, q: str) -> dict | None:
    """Pick the best name match: exact > startswith > substring (all
    case-insensitive). Returns None if nothing contains the query."""
    ql = q.strip().lower()
    exact = [x for x in items if (x.get(name_key) or "").lower() == ql]
    if exact:
        return exact[0]
    starts = [x for x in items if (x.get(name_key) or "").lower().startswith(ql)]
    if starts:
        return starts[0]
    contains = [x for x in items if ql in (x.get(name_key) or "").lower()]
    return contains[0] if contains else None


_MODE_CMD = {"now": "play_now", "queue": "queue", "next": "play_next"}


async def _match_folders(query: str, max_depth: int = 8) -> list[str]:  # #5473
    """Topmost indexed folders whose path contains `query` (case-insensitive).

    Walks the folder tree from the music roots; a match's subfolders are not
    listed separately (the recursive folder read already includes them).
    """
    ql = query.strip().lower().replace("\\", "/")
    found: list[str] = []
    frontier = [n["path"] for n in (await _get("/api/music/folders")).get("folders") or []]
    for _ in range(max_depth):
        nxt: list[str] = []
        for path in frontier:
            if ql and ql in path.lower():
                found.append(path)
                continue
            try:
                listing = await _get(f"/api/music/folders?path={quote(path, safe='')}")
            except httpx.HTTPStatusError:
                continue
            nxt.extend(n["path"] for n in listing.get("folders") or [])
        if not nxt:
            break
        frontier = nxt
    return found


async def _resolve_source(kind: str, query: str, recursive: bool = True) -> tuple[str, list[dict]]:
    """Resolve one (kind, query) to (label, tracks) over the catalog API.

    Shared by dj_queue_by and dj_mix. Raises LookupError with a sayable
    message when nothing matches, PermissionError on a 401.
    """
    if kind == "artist":
        artists = await _get("/api/music/artists")
        m = _best_match(artists, "name", query)
        if not m:
            raise LookupError(f"No artist matching '{query}'.")
        label = f"artist '{m['name']}'"
        tracks = await _get(f"/api/music/artists/{m['id']}/tracks")
    elif kind == "album":
        albums = await _get("/api/music/albums")
        m = _best_match(albums, "title", query)
        if not m:
            raise LookupError(f"No album matching '{query}'.")
        artist = m.get("artist_name")
        label = f"album '{m['title']}'" + (f" by {artist}" if artist else "")
        detail = await _get(f"/api/music/albums/{m['id']}")
        tracks = detail.get("tracks", [])
    elif kind == "genre":
        genres = await _get("/api/music/genres")
        m = _best_match(genres, "name", query)
        if not m:
            raise LookupError(f"No genre matching '{query}'.")
        label = f"genre '{m['name']}'"
        tracks = await _get(f"/api/music/genres/{quote(m['name'], safe='')}/tracks")
    elif kind == "playlist":
        playlists = await _get("/api/playback/playlists")
        m = _best_match(playlists, "name", query)
        if not m:
            raise LookupError(f"No playlist matching '{query}'.")
        label = f"playlist '{m['name']}'"
        detail = await _get(f"/api/playback/playlists/{m['id']}")
        tracks = detail.get("tracks", [])
    elif kind == "folder" and not recursive:  # #5473: loose files only, no subfolders
        try:
            listing = await _get(f"/api/music/folders?path={quote(query, safe='')}")
        except httpx.HTTPStatusError:
            listing = {}
        tracks = []
        for album in listing.get("albums") or []:
            tracks.extend((await _get(f"/api/music/albums/{album['id']}")).get("tracks", []))
        label = f"loose files in '{query}'"
    elif kind == "folder":
        tracks = await _get(f"/api/music/folders/tracks?path={quote(query, safe='')}")
        label = f"folder '{query}'"
    elif kind == "folder_match":  # #5473: every indexed folder whose path contains query
        paths = await _match_folders(query)
        tracks, seen = [], set()
        for path in paths:
            for t in await _get(f"/api/music/folders/tracks?path={quote(path, safe='')}"):
                if t["id"] not in seen:
                    seen.add(t["id"])
                    tracks.append(t)
        label = f"folders matching '{query}' ({len(paths)} folder(s))"
    elif kind == "favorites":
        favorites = await _get("/api/playback/favorites?entity_type=track")
        label = "favorite tracks"
        track_ids_str = [f["entity_key"] for f in favorites]
        tracks = [{"id": int(tid)} for tid in track_ids_str if tid.isdigit()]
    elif kind == "bucket":  # #5518: a themed bucket (audiplex_mcp/buckets.py) as a source
        from audiplex_mcp import buckets  # #5518
        try:  # #5518
            b = buckets.find_bucket(query)  # #5518
        except ValueError as e:  # #5518: ambiguous name
            raise LookupError(str(e)) from None  # #5518
        if not b:  # #5518
            raise LookupError(f"No bucket matching '{query}'.")  # #5518
        label = f"bucket '{b['name']}'"  # #5518
        tracks = [{"id": t["track_id"], "path": t["path"]} for t in b["tracks"]]  # #5518
    elif kind == "search":  # #2806: every music track whose title/artist has all the words
        terms = query.lower().split()
        if not terms:
            raise LookupError("A 'search' source needs words to match.")
        tracks = [t for t in await _all_music_tracks() if not _is_longform(t) and all(
            w in f"{t.get('title') or ''} {t.get('artist_name') or ''}".lower() for w in terms)]
        label = f"search '{query}'"
    elif kind == "tag":  # #2806: every track a DJ tagged with this mood/vibe
        rows = await _get(f"/api/playback/tags/{quote(query.strip(), safe='')}")
        if not rows:
            raise LookupError(f"No tracks tagged '{query}'. dj_tags() lists the tags.")
        label, tracks = f"tag '{query}'", [{"id": r["track_id"]} for r in rows]
    elif kind == "tracks":  # #2806: explicit ids, "12, 34 56"
        ids = [int(x) for x in re.findall(r"\d+", query)]
        if not ids:
            raise LookupError("A 'tracks' source needs track ids in query, e.g. '12, 34'.")
        label, tracks = f"{len(ids)} track id(s)", [{"id": i} for i in ids]
    else:
        raise LookupError(
            f"Unknown kind '{kind}'. Use 'artist', 'album', 'genre', 'folder', "  # #5473 #5518 #2806
            "'folder_match', 'playlist', 'favorites', 'bucket', 'search', 'tag', or 'tracks'."
        )
    return label, tracks


@mcp.tool()
async def dj_queue_by(
    query: str,
    kind: str = "artist",
    mode: str = "queue",
    limit: int = 100,
) -> str:
    """Resolve a NAME to tracks and play/queue them — no need to look up IDs.

    query: the name to match (case-insensitive: exact > prefix > substring).
           Ignored for kind='favorites' (there's exactly one favorites list).
    kind:  'artist' | 'album' | 'genre' | 'folder' | 'playlist' | 'favorites'.
           For 'folder', query is a folder PATH as returned by dj_library —
           this is the one that works on an untagged library, where the
           artist/genre axes are empty. playlist/favorites resolve against the
           configured DJ owner's library (dj_owner_username), not the caller's
           own — the dj-agent service account has none of its own.
    mode:  'now' (replace current & play), 'queue' (append to end, default),
           'next' (insert after the current track).
    limit: max tracks to enqueue (default 100).

    Resolution is done here in the MCP server over the catalog REST API
    (there is no dedicated /search endpoint). Reports which entity it matched.
    """
    cmd_type = _MODE_CMD.get(mode)
    if cmd_type is None:
        return f"Unknown mode '{mode}'. Use 'now', 'queue', or 'next'."
    try:
        label, tracks = await _resolve_source(kind, query)
    except (PermissionError, LookupError) as e:
        return str(e)

    track_ids = [t["id"] for t in tracks][: max(0, limit)]
    if not track_ids:
        return f"Matched {label} but it has no tracks."
    data = await _enqueue(cmd_type, {"track_ids": track_ids})
    if isinstance(data, str):
        return _held_result(data)  # #ride0928
    verb = {"now": "Sent to play now", "queue": "Queued", "next": "Sent to play next"}[mode]  # #2843
    return await _result(data, len(track_ids)) + (  # #ride0928
        f"{verb} {_sent_count(data, len(track_ids))} track(s) from {label} "
        f"(command #{data.get('id')}, {data.get('pending')} pending)."
    ) + _missing_note(data)  # #3249



async def _await_ack(command_id: int, timeout: float = 12.0) -> dict | None:
    """The registry row for one command once the device acks it, else None."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        try:
            for row in await _get("/api/playback/commands"):
                if row.get("id") == command_id and row.get("ack_status"):
                    return row
        except Exception:
            pass
        await asyncio.sleep(1.0)
    return None


# ----- #5463: balanced mixes, recent-play exclusion, old-APK boundary swap -----
#
# 2026-09-28: Jarvis called dj_mix once per source. The phone build lacked
# replace_upcoming, so each call APPENDED that source after the old tail: 992
# Ren Faire tracks, then 277 YouTube, i.e. hours of one folder. Now every add
# re-plans the WHOLE upcoming list, interleaves sources, drops recent plays,
# and on an old build swaps the queue in at a song boundary instead.

SWAP_NEAR_END_MS = 1500  # #5463: fire when the current song has this little left
SWAP_START_GRACE_MS = 3000  # #5463: or within this far into the next song
SWAP_MIN_WAIT_S = 20 * 60  # #5463
SWAP_POLL_S = 1.0  # #5463
_SWAP: dict = {"task": None, "status": "none", "detail": "", "ids": []}  # #5463


def _mix_sources_path() -> Path:  # #5463
    default = Path(__file__).resolve().parent.parent / "data" / "dj_mix_sources.json"
    return Path(os.environ.get("DJ_MIX_SOURCES_FILE") or default)


def _load_source_map() -> dict[int, str]:  # #5463
    """Which source each queued track came from, shared by every persona's MCP."""
    try:
        raw = json.loads(_mix_sources_path().read_text(encoding="utf-8"))
        return {int(k): str(v) for k, v in raw.items()}
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def _save_source_map(source_of: dict[int, str], keep: list[int]) -> None:  # #5463
    path = _mix_sources_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({str(i): source_of[i] for i in keep if i in source_of}),
                        encoding="utf-8")
    except OSError:
        pass  # labels are a nicety; a mix must not fail over them


def _source_label(src: dict) -> str:  # #5463: short, sayable
    q = str(src.get("query", "")).rstrip("/\\")
    return str(src.get("label") or q.replace("\\", "/").split("/")[-1] or src.get("kind", "source"))


async def _recent_split(ids: list[int], hours: float) -> tuple[list[int], int]:  # #5463
    """(ids not played in the last `hours`, how many were dropped). Fail-open."""
    if hours <= 0 or not ids:
        return list(ids), 0
    minutes = hours * 60
    try:
        verdict = await _post("/api/playback/candidates/filter", {
            "track_ids": ids,
            "recording_cooldown_minutes": minutes,
            "work_cooldown_minutes": minutes,
        })
    except Exception:
        return list(ids), 0
    gone = {int(s["track_id"]) for s in verdict.get("suppressed") or []}
    return [i for i in ids if i not in gone], len(gone)


def _speech_busy() -> bool:  # #5463: never swap audio over Todd or a persona clip (#2845)
    """Todd talking/typing, or a persona holds the speaking claim."""
    return _todd_talking(claim=True) in ("talking", "unreadable")  # #ride0928


def _remaining_ms(state: dict) -> int | None:  # #5463
    dur = int(state.get("duration_ms") or 0)
    if dur <= 0:
        return None
    pos = int(state.get("position_ms") or 0)
    if state.get("playing") and state.get("updated_at"):
        import time
        pos += max(0, int((time.time() - float(state["updated_at"])) * 1000))
    return dur - pos


def _swap_set(status: str, detail: str) -> None:  # #5463
    _SWAP["status"], _SWAP["detail"] = status, detail
    print(f"[dj_mix swap] {status}: {detail}", file=sys.stderr, flush=True)


async def _boundary_swap(ids: list[int], start_id: int, max_wait_s: float) -> None:  # #5463
    """Replace the queue with `ids` at the next song boundary, never mid-song.

    Fires when the current song is about to end, or just after it changed (a
    skip is a boundary too). Defers past any boundary where Todd is talking or
    a persona clip / DJ break is playing: that boundary is let go and it waits
    for the next one.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max_wait_s
    failures = 0
    while loop.time() < deadline:
        await asyncio.sleep(SWAP_POLL_S)
        try:
            state = await _get("/api/playback/state")
            failures = 0
        except Exception as e:
            failures += 1
            if failures >= 30:
                return _swap_set("aborted", f"lost the player state ({e})")
            continue
        track = state.get("track") or {}
        cur = track.get("id")
        if cur is None:
            return _swap_set("aborted", "the player stopped; nothing sent")
        if cur == -1:
            return _swap_set("aborted", "a live stream started; nothing sent")
        rem = _remaining_ms(state)
        near_end = bool(state.get("playing")) and rem is not None and rem <= SWAP_NEAR_END_MS
        just_changed = cur != start_id and int(state.get("position_ms") or 0) <= SWAP_START_GRACE_MS
        if cur != start_id and not just_changed:
            start_id = cur  # missed that boundary (poll gap); wait for the next
            continue
        if not (near_end or just_changed):
            continue
        if cur < 0 or _speech_busy():
            start_id = cur  # #2845: never over talk or a persona clip; next boundary
            _swap_set("pending", "deferred a boundary: Todd talking or a persona clip playing")
            continue
        send = [i for i in ids if i != cur]
        data = await _enqueue("play_now", {"track_ids": send})
        if isinstance(data, str):
            return _swap_set("failed", data)
        return _swap_set("fired", f"play_now command #{data.get('id')}, "
                                  f"{_sent_count(data, len(send))} track(s) in {data.get('chunks', 1)} chunk(s)")
    _swap_set("aborted", "no song boundary within the wait window; nothing sent")


@mcp.tool()
async def dj_mix_status() -> str:
    """Did dj_mix's pending queue swap (old phone builds) fire yet? (#5463)"""
    task = _SWAP.get("task")
    live = task is not None and not task.done()
    return (f"Swap {_SWAP['status']}{' (waiting for the song to end)' if live else ''}: "
            f"{_SWAP['detail'] or 'nothing scheduled'}. {len(_SWAP['ids'])} track(s) in the planned mix.")


@mcp.tool()
async def dj_mix(
    sources: list[dict] | None = None,
    track_ids: list[int] | None = None,
    track_ids_file: str = "",
    shuffle: bool = True,
    seed: int | None = None,
    balance: str = "even",
    exclude_recent_hours: float = 12,
    allow_empty: bool = False,
    keep_upcoming: bool = True,  # #2806
) -> str:
    """Build or extend a shuffled mix from several sources — Todd's standing order.

    On every add: trim what already played, dedupe across folders (same
    recording = one entry), reshuffle everything still to come, and NEVER
    interrupt the current song — the new mix goes in after it.

    Pass all sources in one call when you can. Either way the whole upcoming
    list is re-planned each call, so a second call re-mixes rather than
    appending a block (#5463).

    sources:        [{"kind": "folder", "query": "<path>"}, {"kind": "artist",
                    "query": "Heart"}, ...] — same kinds as dj_queue_by. An
                    optional "label" names the source in the reply.
    track_ids:      explicit Audiplex track ids to add as well.
    track_ids_file: path to a JSON list of track ids (e.g. a saved shuffle).
    shuffle:        reshuffle the whole upcoming tail (default True).
    seed:           make the shuffle repeatable.
    balance:        how sources share the queue (#5463). "even" (DEFAULT) =
                    round-robin, one track per source in turn, so a 1006-track
                    folder and a 277-track folder alternate 1:1 until the small
                    one runs out — Todd asked for a broad mix. "proportional" =
                    spread evenly by pool size (~80/20 for those two).
                    "none" = one-pot shuffle. Tracks already queued keep their
                    source; unknown ones count as one "already queued" source.
    allow_empty:    by default a named source that resolves to 0 tracks makes
                    the whole call REFUSE (nothing sent), naming the empty
                    source (#5473: faster/slower silently missing on a ride).
                    Sources may also carry "recursive": false (loose files in
                    that folder only) or use kind "folder_match" (every indexed
                    folder whose path contains the query).
    keep_upcoming:  False = the new tracks REPLACE what's queued after the
                    current song instead of mixing into it (#2806; dj_energy_set).
    exclude_recent_hours: drop tracks Todd heard in the last N hours
                    (default 12; 0 = off). Explicit dj_play_now/dj_queue never
                    filter — a song asked for by name plays.

    Nothing loaded on the phone → starts the mix with play_now. A live stream
    playing → refuses (a stream has no queue to add after). An old phone build
    without replace_upcoming → the full mix replaces the queue via play_now at
    the NEXT SONG BOUNDARY (never mid-song, never over Todd talking or a
    persona clip); dj_mix_status() says when it fired. Paused on an old build
    → nothing is sent (play_now would start music).
    """
    if balance not in ("even", "proportional", "none"):  # #5463
        return f"Unknown balance '{balance}'. Use 'even', 'proportional', or 'none'."
    new_ids: list[int] = list(track_ids or [])
    labels: list[str] = []
    source_of = _load_source_map()  # #5463
    empty: list[str] = []  # #5473
    try:
        for src in sources or []:
            try:
                label, tracks = await _resolve_source(
                    str(src.get("kind", "folder")), str(src.get("query", "")),
                    recursive=bool(src.get("recursive", True)),  # #5473
                )
            except LookupError as e:  # #5473: a no-match is an empty source, named
                label, tracks = f"{src.get('kind', 'folder')} '{src.get('query', '')}' ({e})", []
            except httpx.HTTPStatusError:  # #5473: unknown folder path
                label, tracks = f"{src.get('kind', 'folder')} '{src.get('query', '')}'", []
            labels.append(f"{label} ({len(tracks)})")
            if not tracks:
                empty.append(label)
            short = _source_label(src)  # #5463
            for t in tracks:
                new_ids.append(int(t["id"]))
                source_of[int(t["id"])] = short
    except (PermissionError, LookupError) as e:
        return str(e)
    if track_ids_file:
        try:
            loaded = json.loads(Path(track_ids_file).read_text(encoding="utf-8"))
            new_ids.extend(int(i) for i in loaded)
            labels.append(f"{Path(track_ids_file).name} ({len(loaded)})")
            for i in loaded:  # #5463
                source_of.setdefault(int(i), Path(track_ids_file).stem)
        except (OSError, ValueError, TypeError) as e:
            return f"Couldn't read track_ids_file: {e}"
    if track_ids:
        labels.append(f"{len(track_ids)} explicit id(s)")
        for i in track_ids:  # #5463
            source_of.setdefault(int(i), "requested")
    if empty and not allow_empty:  # #5473: never report success with a source missing
        return ("REFUSED, nothing sent: " + "; ".join(f"{e}: 0 tracks" for e in empty)
                + " (not in the library index, or the name/path is wrong). Per source: "
                + ", ".join(labels) + ". Fix the source, or pass allow_empty=True to mix without it.")
    if not new_ids:
        return "Nothing to mix: no sources, track_ids or track_ids_file resolved to tracks."
    # #5495 item 4: a hand-built mix replaces the rolling pool (server-owned, so over HTTP)
    pool_stopped = ""
    try:
        if (await _delete("/api/playback/pool")).get("stopped"):
            pool_stopped = " (stopped the rolling pool)."
    except Exception:
        pass  # server unreachable: the mix itself still goes out

    try:
        state = await _get("/api/playback/state")
    except PermissionError as e:
        return str(e)
    track = state.get("track") or {}
    if track.get("id") == -1:
        return "A live stream is playing; dj_mix works on the music queue. Nothing sent."
    # "Loaded" is judged on the raw queue: a DJ voice break (negative id) is
    # still the current item and must not be interrupted by a play_now.
    raw_queue = state.get("queue") or []
    loaded_now = track.get("id") is not None and bool(raw_queue)
    queue = [q for q in raw_queue if (q.get("id") or 0) > 0]
    idx = state.get("queue_index") or 0
    upcoming_ids = [q["id"] for q in queue if q.get("index", 0) > idx] if loaded_now and keep_upcoming else []  # #5463 #2806
    pending = _SWAP.get("task")
    if loaded_now and keep_upcoming and pending is not None and not pending.done():  # #2806
        upcoming_ids = list(_SWAP["ids"])  # #5463: a not-yet-fired swap IS the queue to come
    recent_note = ""  # #5463
    kept_new, dropped_new = await _recent_split(new_ids, exclude_recent_hours)
    kept_tail, dropped_tail = await _recent_split(upcoming_ids, exclude_recent_hours)
    if kept_new or kept_tail:
        new_ids, upcoming_ids = kept_new, kept_tail
        if dropped_new + dropped_tail:
            recent_note = f" Left out {dropped_new + dropped_tail} track(s) heard in the last {exclude_recent_hours:g}h."
    elif dropped_new:
        recent_note = f" Everything was heard in the last {exclude_recent_hours:g}h, so the recent filter was skipped."
    body = {"new_ids": new_ids, "shuffle": shuffle, "seed": seed}
    if loaded_now:
        body |= {
            "current_id": track["id"],
            "played_ids": [q["id"] for q in queue if q.get("index", 0) < idx],
            "upcoming_ids": upcoming_ids,  # #5463
        }
    try:
        plan = await _post("/api/playback/mix/plan", body)
    except PermissionError as e:
        return str(e)
    upcoming = plan.get("upcoming") or []
    if shuffle and balance != "none":  # #5463
        upcoming = balance_order(upcoming, source_of, balance, seed)
    _save_source_map(source_of, upcoming)  # #5463
    _SWAP["ids"] = list(upcoming)
    mixed = f" {balance.capitalize()} balance, {describe_head(upcoming, source_of)}." if upcoming else ""  # #5463
    head = f"Mix from {', '.join(labels)}: {plan.get('summary')}.{recent_note}{mixed}{pool_stopped}"  # #5463, #5495 item 4
    if plan.get("skipped_long"):  # #3249: say so, so a wanted long track isn't silently gone
        head += (f" Left out {len(plan['skipped_long'])} track(s) over 20 min"
                 f" (ids {plan['skipped_long'][:5]}); dj_play_next them by id if wanted.")
    if loaded_now and not state.get("playing"):  # #3249: 13:17 mixed into a stopped player
        head += (" NOTE: the player is loaded but NOT playing, so this mix will sit"
                 " there silently until you dj_resume (announce first).")

    if not loaded_now:
        if not upcoming:
            return head + " Nothing left to play."
        data = await _enqueue("play_now", {"track_ids": upcoming})
        if isinstance(data, str):
            return _held_result(data)  # #ride0928
        return (await _result(data, len(upcoming))  # #ride0928
                + head + f" Nothing was loaded, so it was sent to start now (command #{data.get('id')})." + _missing_note(data))  # #3249 #2843

    if not await _device_lacks_replace_upcoming():  # #3249: don't re-send a known failure
        # Only the first chunk rides replace_upcoming; the rest is appended once
        # the phone has said yes (an unknown_type must not leave stray appends).
        first, tail = upcoming[:QUEUE_CHUNK], upcoming[QUEUE_CHUNK:]  # #3249
        data = await _enqueue("replace_upcoming", {"track_ids": first})
        if isinstance(data, str):
            return _held_result(data)  # #ride0928
        ack = await _await_ack(int(data["id"]))
        titles = json.dumps(await _titles(list(data.get("dropped_missing") or [])), ensure_ascii=False)  # #ride0928
        ack_txt = "none_yet" if ack is None else f"{ack.get('ack_status')}" + (f": {ack['ack_detail']}" if ack.get("ack_detail") else "")
        head = f"RESULT sent={_sent_count(data, len(first))} held=no skipped_missing={titles} phone_ack={ack_txt}\n" + head + _missing_note(data)  # #ride0928
        if ack is None:
            return head + (
                f" Sent replace_upcoming (command #{data['id']}) after the current song;"
                " no ack yet — check dj_command_status."
                + (f" {len(tail)} more track(s) NOT sent yet; add them once it acks." if tail else "")
            )
        if ack.get("ack_status") == "ok":
            more = await _enqueue("queue", {"track_ids": tail}) if tail else None  # #3249
            extra = f" plus {_sent_count(more, len(tail))} appended" if isinstance(more, dict) else ""
            return head + f" Queued after the current song (command #{data['id']}){extra}."
        if ack.get("ack_status") != "unknown_type":
            return head + f" The phone refused it: {ack.get('ack_status')} {ack.get('ack_detail') or ''}".rstrip()

    # #5463: older phone build without replace_upcoming. Appending only the new
    # tracks is what segregated the sources, so the WHOLE planned list replaces
    # the queue via play_now — at the next song boundary, never mid-song.
    old = _SWAP.get("task")
    if old is not None and not old.done():
        old.cancel()  # a newer mix supersedes a pending swap
    if not upcoming:
        return head + " (Old phone build: nothing left to swap in; queue left as it is.)"
    if not state.get("playing"):
        return head + (" Old phone build can't reshuffle in place and the player is paused;"
                       " swapping the queue would start music, so NOTHING was sent. Announce,"
                       " dj_resume, then call dj_mix again.")
    rem = _remaining_ms(state)
    wait = max(SWAP_MIN_WAIT_S, (rem or 0) / 1000 + 60)
    _SWAP["task"] = asyncio.create_task(_boundary_swap(list(upcoming), int(track["id"]), wait))
    _swap_set("pending", f"{len(upcoming)} track(s) replace the queue when the current song ends")
    when = f" (~{max(0, rem) // 1000}s)" if rem is not None else ""
    return head + (
        f" Old phone build can't reshuffle in place, so the full mix of {len(upcoming)} track(s)"
        f" replaces the queue when the current song ends{when}, never over Todd talking or a"
        " persona clip. Check dj_mix_status()."
    )


def _describe_age(seconds: float | None) -> str:
    if seconds is None:
        return "never"
    if seconds < 60:
        return f"{int(seconds)}s ago"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    return f"{seconds / 3600:.1f}h ago"


def _describe_device(d: dict) -> str:
    """One line on whether a player is actually out there (#2961)."""
    if not d.get("connected"):
        last = _describe_age(d.get("last_poll_age_seconds"))
        pending = d.get("pending", 0)
        tail = f" {pending} command(s) waiting for it." if pending else ""
        if d.get("last_poll_at") is None:
            return f"NO PLAYER CONNECTED (none has ever polled this server).{tail}"
        return f"NO PLAYER CONNECTED (last poll {last}).{tail}"
    return f"Player connected (last poll {_describe_age(d.get('last_poll_age_seconds'))})."


@mcp.tool()
async def dj_device_status() -> str:
    """Is an Audiplex player actually alive and listening right now?

    Answers the question dj_now_playing cannot: a device that is connected but
    idle and a device that is dead both report nothing playing. Use this before
    concluding a play command failed.
    """
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{AUDIPLEX_URL}/api/playback/device", headers=_headers()
        )
    if resp.status_code == 401:
        return "Auth failed (401). Check AUDIPLEX_TOKEN."
    resp.raise_for_status()
    d = resp.json()
    lines = [_describe_device(d)]
    if d.get("last_command_id"):
        lines.append(
            f"Last command taken: #{d['last_command_id']} ({d.get('last_command_type')}) "
            f"{_describe_age(d.get('last_command_delivered_age_seconds'))}."
        )
    if d.get("ever_reported_state"):
        lines.append(
            f"Last now-playing report: {_describe_age(d.get('last_state_age_seconds'))}."
        )
    else:
        lines.append("Last now-playing report: never (no state has ever been reported).")
    if d.get("pending"):
        lines.append(f"{d['pending']} command(s) still queued.")
    return "\n".join(lines)


def _describe_devices(payload: dict) -> list[str]:
    active = payload.get("active_device_id")
    devices = payload.get("devices") or []
    if not devices:
        return ["No renderers registered (no device has polled this server)."]
    lines = []
    for d in devices:
        mark = "* " if d.get("active") else "  "
        live = "live" if d.get("connected") else f"stale {_describe_age(d.get('last_seen_age_seconds'))}"
        lines.append(f"{mark}{d.get('id')} ({d.get('name')}, {d.get('type')}) — {live}")
    if active is None:
        lines.append("Active: none (any live renderer receives commands — the phone).")
    return lines


@mcp.tool()
async def dj_devices() -> str:
    """List the renderers registered on the bus and which one is ACTIVE.

    The active device is the one dj_play_now / dj_queue / etc. currently drive.
    A '*' marks it. Use dj_transfer to hand playback to a different device
    (the phone, or a Windows PC running the follow-me client)."""
    payload = await _get("/api/playback/devices")
    return "\n".join(_describe_devices(payload))


@mcp.tool()
async def dj_transfer(device: str) -> str:
    """Transfer playback to a device — Spotify-Connect-style handoff.

    `device` matches a device id or friendly name (case-insensitive), e.g.
    'phone', 'pc-solace', 'Solace'. The music moves with it: the previous
    device pauses and reports its position, then the new one resumes the same
    queue from that exact spot (voice clips and streams don't carry over), and
    DJ commands drive the new device from then on. If the target goes stale
    (PC asleep/closed), playback falls back to the phone.
    """
    payload = await _get("/api/playback/devices")
    devices = payload.get("devices") or []
    key = device.strip().lower()
    match = None
    for d in devices:
        if key in (str(d.get("id", "")).lower(), str(d.get("name", "")).lower()):
            match = d
            break
    # 'phone' is always a valid target even if it has not polled yet.
    target_id = match["id"] if match else ("phone" if key == "phone" else None)
    if target_id is None:
        known = ", ".join(f"{d.get('id')} ({d.get('name')})" for d in devices) or "none"
        return f"No device matches '{device}'. Registered: {known}."
    held = _talk_hold("activate")  # #ride0928: the target resumes the queue = music starts
    if held:
        return held
    try:  # #2843: only a transfer of something PLAYING should end up playing
        was_playing = bool((await _get("/api/playback/state") or {}).get("playing"))
    except Exception:
        was_playing = False
    since = time.time()
    result = await _post(f"/api/playback/devices/{target_id}/activate", {})
    lines = [f"Transferred playback to '{target_id}'."]
    if was_playing:  # #2843
        verdict, why, _ = await _verify_start(
            None, "activate", {}, state_path=f"/api/playback/state?device_id={quote(target_id)}",
            since=since)
        head = f"RESULT playing={verdict} on '{target_id}'"
        lines.insert(0, head if verdict == "YES"
                     else f"{head}\nPLAYING={verdict}: {why}. {NOT_YES_TEXT[verdict]}")
    lines.extend(_describe_devices(result))
    return "\n".join(lines)


@mcp.tool()
async def dj_client_log(limit: int = 25) -> str:
    """Recent diagnostics shipped up by the Android player — playback errors and
    process-exit reasons. This is how you find out WHY audio stopped or the app
    died; the phone is not reachable from the server host any other way."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{AUDIPLEX_URL}/api/playback/client-log",
            headers=_headers(),
            params={"limit": max(1, min(limit, 200))},
        )
    if resp.status_code == 401:
        return "Auth failed (401). Check AUDIPLEX_TOKEN."
    resp.raise_for_status()
    entries = resp.json()
    if not entries:
        return "No client diagnostics reported."
    return _render_client_log(entries)


def _render_client_log(entries: list) -> str:
    """One line per entry, except a stack trace, which gets its own block.

    A trace inlined into the comma-joined detail dict is unreadable — which is
    part of why traces were not being shipped at all before #3021. Pull it out
    and indent it instead.
    """
    lines = []
    for e in entries:
        when = datetime.datetime.fromtimestamp(e.get("received_at", 0)).strftime("%H:%M:%S")
        line = f"[{when}] {e.get('level', 'info').upper()} {e.get('event')}: {e.get('message', '')}"
        detail = dict(e.get("detail") or {})
        trace = detail.pop("trace", "")
        if detail:
            line += " " + ", ".join(f"{k}={v}" for k, v in detail.items())
        lines.append(line.rstrip())
        if trace:
            lines.extend("    " + t for t in str(trace).splitlines())
    return "\n".join(lines)


@mcp.tool()
async def dj_client_exits(limit: int = 25) -> str:
    """Process-exit reports that SURVIVED a server restart.

    dj_client_log reads an in-memory ring buffer, so a restart wipes it — and
    the phone advances its own report watermark the moment the server accepts
    an entry, so it never re-sends one. That makes a death report the one
    diagnostic that can be lost for good, which is why it is also written to
    disk (#3021). Reach for this when dj_client_log looks emptier than it
    should, or when you need history older than the buffer holds."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{AUDIPLEX_URL}/api/playback/client-exits",
            headers=_headers(),
            params={"limit": max(1, min(limit, 200))},
        )
    if resp.status_code == 401:
        return "Auth failed (401). Check AUDIPLEX_TOKEN."
    resp.raise_for_status()
    entries = resp.json()
    if not entries:
        return "No process exits on record."
    return _render_client_log(entries)


@mcp.tool()
async def dj_track_ratings(limit: int = 30) -> str:
    """Todd's OWN star ratings for tracks in the library, best first (#3024).

    Distinct from dj_taste: that tracks how your RECOMMENDATIONS landed
    (good/meh on things not in the library). This is Todd rating tracks he is
    listening to, 1-5 stars, from the now-playing screen in the app. Read it
    before building a set — a 5 is the strongest "play this again" signal
    available, and a 1 is the clearest "don't"."""
    ratings = await _get("/api/playback/ratings")
    if not ratings:
        return (
            "No tracks rated yet. The star control is on the now-playing screen "
            "in the app; nothing to weight picks with until he uses it."
        )
    lines = ["Todd's track ratings (his own stars, best first):"]
    by_id = {r["track_id"]: r for r in ratings[:limit]}
    for track_id, r in by_id.items():
        stars = "*" * r["rating"]
        label = f"track {track_id}"
        try:
            t = await _get(f"/api/music/tracks/{track_id}")
            label = f"{t.get('artist_name', '')} - {t.get('title', '')}".strip(" -") or label
        except Exception:
            pass
        line = f"  [{stars:<5}] {label}"
        if r.get("note"):
            line += f'  — "{r["note"]}"'
        lines.append(line)
    return "\n".join(lines)


@mcp.tool()
async def dj_check_picks(track_ids: list[int], min_rating: int = 0) -> str:
    """Before you queue a set: which of these would repeat something Todd just
    heard, and why (#948).

    Two windows, both 20 minutes by default. The first is the same RECORDING —
    he just heard this exact track. The second is the same SONG in any version:
    a live cut, a remaster, the same tune off a different album still counts as
    hearing it twice.

    ADVISORY ONLY. Nothing here blocks playback and nothing is removed from a
    queue behind your back — if Todd asked for a song by name, play it. This
    exists so YOUR OWN picks don't repeat themselves, and so you can say why
    you passed something over instead of silently narrowing his library.

    min_rating: optionally also flag tracks rated below N stars (1-5). 0 = off.
    """
    if not track_ids:
        return "No track_ids given; nothing to check."
    body: dict = {"track_ids": track_ids}
    if min_rating:
        body["min_rating"] = min_rating
    verdict = await _post("/api/playback/candidates/filter", body)

    allowed = verdict.get("allowed") or []
    suppressed = verdict.get("suppressed") or []
    lines = [
        f"Checked {len(track_ids)} pick(s) against a "
        f"{verdict.get('recording_cooldown_minutes')}-min recording / "
        f"{verdict.get('work_cooldown_minutes')}-min song cooldown."
    ]
    lines.append(f"  Clear to play ({len(allowed)}): {allowed if allowed else 'none'}")
    if suppressed:
        lines.append(f"  Would repeat ({len(suppressed)}):")
        lines.extend(_describe_suppression(s) for s in suppressed)
        lines.append(
            "  Swap those out if you're free-picking. If Todd asked for one by "
            "name, play it anyway and just say you know he heard it recently."
        )
    return "\n".join(lines)


@mcp.tool()
async def dj_track_stats(limit: int = 25, min_starts: int = 2) -> str:
    """How Todd actually listens to tracks he has played: completion RATE and
    where the skips land (#947).

    Sharper than dj_most_played's raw counts. "Played eight times, finished
    twice" and "played twice, finished twice" have identical play counts and
    opposite meanings — the rate separates them. The mean/median skip position
    tells you the other half: bailing at 4 seconds is "wrong song", bailing at
    four minutes is "good song, too long for right now".

    One honest limit: a 'complete' is posted with the track's full duration, so
    it means REACHED THE END, not heard every second of it. Treat it as taste,
    not as proof of attention.

    Statistics pool across every copy of a recording, so a track that exists
    both on the server and on the phone doesn't look half-listened-to twice.
    """
    stats = await _get(f"/api/playback/track-stats?limit={limit}&min_starts={min_starts}")
    if not stats:
        return (
            "No listening history yet. Play stats accumulate as Todd listens; "
            "until then, dj_track_ratings (his stars) is the signal to use."
        )
    lines = ["How Todd listens (completion rate, then where he bails):"]
    for entry in stats:
        track = entry.get("track") or {}
        label = f"{track.get('artist_name') or ''} - {track.get('title') or ''}".strip(" -")
        rate = entry.get("completion_rate")
        rate_text = "no starts yet" if rate is None else f"{rate * 100:.0f}% finished"
        line = (
            f"  {label or 'track ' + str(track.get('id'))}: {rate_text} "
            f"({entry.get('completes')}/{entry.get('starts')} plays)"
        )
        if entry.get("abandons"):
            line += (
                f", {entry['abandons']} bail(s) around "
                f"{entry.get('median_skip_seconds')}s"
            )
            if entry.get("early_skips"):
                line += f", {entry['early_skips']} of them in the first 10s"
        if len(entry.get("track_ids") or []) > 1:
            line += f"  [{len(entry['track_ids'])} copies pooled]"
        lines.append(line)
    return "\n".join(lines)


@mcp.tool()
async def dj_now_playing() -> str:
    """Report what the Audiplex device is currently playing — track, artist,
    play/pause state, position — plus the full current queue (with indices,
    for dj_reorder), as last reported by the client.

    Also reports device liveness, so 'nothing playing' can be told apart from
    'nothing listening' (#2961)."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{AUDIPLEX_URL}/api/playback/state", headers=_headers()
        )
        if resp.status_code == 401:
            return "Auth failed (401). Check AUDIPLEX_TOKEN."
        resp.raise_for_status()
        device_resp = await client.get(
            f"{AUDIPLEX_URL}/api/playback/device", headers=_headers()
        )
    s = resp.json()
    device = device_resp.json() if device_resp.status_code == 200 else {}
    device_line = _describe_device(device) if device else ""
    track = s.get("track")
    if not track:
        why = (
            "The player is connected and idle — nothing is loaded."
            if device.get("connected")
            else "No player has reported state."
        )
        errs = "\n".join(await _player_error_lines())  # #3249
        stop = s.get("stop") or {}
        if stop.get("job") or stop.get("latched"):  # #3505
            errs = _describe_stop(stop) + "\n" + errs
        return f"Nothing is playing. {why}\n{device_line}\n{errs}".rstrip()
    age = device.get("last_state_age_seconds")
    if age is None and s.get("updated_at"):  # #3249: age from the snapshot itself
        import time
        age = time.time() - float(s["updated_at"])
    if age is not None and age > 60:
        # Stale state is worse than no state: it reads as live and isn't.
        device_line += (
            f"\nWARNING: this snapshot is {_describe_age(age)} — "
            "the player may have stopped or died since."
        )
    state = "playing" if s.get("playing") else "paused"
    pos = int(s.get("position_ms", 0) // 1000)
    dur = int(s.get("duration_ms", 0) // 1000)
    idx = s.get("queue_index", 0)
    lines = [
        f"{state}: {track.get('title')} - {track.get('artist')} "
        f"[{pos // 60}:{pos % 60:02d}/{dur // 60}:{dur % 60:02d}] "
        f"(queue {idx + 1}/{s.get('queue_length', 0)})"
    ]
    if s.get("volume") is not None:
        lines.append(f"volume: {round(s['volume'] * 100)}%")
    queue = s.get("queue") or []
    if queue:
        lines.append("Queue:")
        for item in queue:
            marker = "> " if item.get("index") == idx else "  "
            lines.append(
                f"{marker}{item.get('index')}: {item.get('title')} - {item.get('artist')}"
            )
    if device_line:
        lines.append(device_line)
    if s.get("app_version_name"):  # #3505
        lines.append(f"Phone app: {s['app_version_name']}")
    stop = s.get("stop") or {}
    if stop.get("job") or stop.get("latched"):  # #3505 (Jarvis rider 3)
        lines.append(_describe_stop(stop))
    lines += await _player_error_lines()  # #3249
    miss = await _missing_on_phone()  # #ride0928
    if miss:
        lines.append(miss)
    active = device.get("active_device_id")
    effective = device.get("effective_target_device_id")
    if active is not None:
        note = f"Active device: {active}"
        if effective is None:
            note += " (STALE — playback has fallen back to any live renderer, i.e. the phone)"
        lines.append(note)
    return "\n".join(lines)


# --- Catalog browsing (item #2943) -----------------------------------------
#
# Why these are title-first rather than artist/album-first: the live library is
# a flat yt-dlp dump. All 206 tracks scanned into ONE album ("music", q:/music)
# under one artist whose name is the EMPTY STRING, with no genres at all — the
# files carry no embedded tags. So /artists, /albums and /genres are degenerate
# and a DJ browsing them alone learns nothing. The real metadata lives in the
# track TITLE strings, which is why dj_tracks + dj_search carry the weight and
# dj_library's job is partly to say "this axis is empty because the files are
# untagged" instead of letting the agent conclude the library is empty.

LONGFORM_SECONDS = 15 * 60

# yt-dlp leaves the encoding tag and the "official video" family in filenames.
# Stripped for DISPLAY ONLY — track IDs and the server's stored titles are
# untouched, so anything shown here can be passed straight back as an ID.
_CRUFT = re.compile(
    r"""\s*(?:
        \(\s*\d+\s*kbit_[A-Za-z0-9]+\s*\)      # (128kbit_AAC), (152kbit_Opus)
      | [\(\[]\s*(?:official\s+)?
          (?:music\s+video|lyric\s+video|lyrics?\s+video|video|audio|
             visuali[sz]er|hd\s+video|lyrics?)
        \s*[\)\]]
      | [\(\[]\s*official\s*[\)\]]
    )""",
    re.IGNORECASE | re.VERBOSE,
)


def _clean_title(title: str) -> str:
    """Strip yt-dlp noise for readability. Cosmetic only."""
    cleaned = _CRUFT.sub("", title or "")
    return re.sub(r"\s{2,}", " ", cleaned).strip(" -–—") or (title or "")


def _fmt_time(epoch) -> str:
    """Epoch seconds as local wall-clock, for humans reading a timeline."""
    if not isinstance(epoch, (int, float)) or epoch <= 0:
        return "unknown"
    return datetime.datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")


def _fmt_duration(seconds) -> str:
    if not isinstance(seconds, (int, float)) or seconds <= 0:
        return "?:??"
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _is_longform(track: dict) -> bool:
    dur = track.get("duration_seconds")
    return isinstance(dur, (int, float)) and dur >= LONGFORM_SECONDS


def _track_line(track: dict) -> str:
    """`id | title | m:ss` (+ artist when known, + LONG-FORM warning)."""
    artist = (track.get("artist_name") or "").strip()
    title = _clean_title(track.get("title") or "(untitled)")
    label = f"{artist} - {title}" if artist else title
    flag = "  [LONG-FORM — not a song, do not put in a music set]" if _is_longform(track) else ""
    return f"{track.get('id')} | {label} | {_fmt_duration(track.get('duration_seconds'))}{flag}"


async def _all_music_tracks() -> list[dict]:
    """Every track under every music root, de-duplicated by id."""
    listing = await _get("/api/music/folders")
    seen: dict[int, dict] = {}
    for folder in listing.get("folders") or []:
        path = folder.get("path")
        if not path:
            continue
        for t in await _get(f"/api/music/folders/tracks?path={quote(path, safe='')}"):
            seen[t["id"]] = t
    return list(seen.values())


@mcp.tool()
async def dj_library(kind: str = "overview", path: str | None = None) -> str:
    """Survey what's actually IN the library, so you can pick music yourself
    instead of waiting to be told what to play. Read-only; queues nothing.

    kind: 'overview'  (default) — one-shot orientation: music folders with
                       track counts, plus how much each browse axis is worth.
          'folders'   — browse the folder tree; pass `path` to descend.
          'artists' | 'albums' | 'genres' | 'playlists' — list that axis.

    START HERE, then use dj_search / dj_tracks to get the track IDs you feed to
    dj_play_now / dj_queue / dj_play_next.
    """
    try:
        if kind == "overview":
            roots, artists, albums, genres = (
                await _get("/api/music/roots"),
                await _get("/api/music/artists"),
                await _get("/api/music/albums"),
                await _get("/api/music/genres"),
            )
            try:
                playlists = await _get("/api/playback/playlists")
            except httpx.HTTPError:
                playlists = []
            listing = await _get("/api/music/folders")
            folders = listing.get("folders") or []
            total = sum(f.get("track_count", 0) for f in folders)

            lines = [f"MUSIC LIBRARY — {total} track(s) across {len(folders)} folder(s)."]
            for f in folders:
                lines.append(
                    f"  {f.get('name')}  ({f.get('track_count')} tracks, "
                    f"{f.get('album_count')} album(s))  path={f.get('path')}"
                )
            missing = [r["path"] for r in roots.get("roots", []) if not r.get("exists")]
            if missing:
                lines.append(f"  (configured but not on disk right now: {', '.join(missing)})")

            named_artists = [a for a in artists if (a.get("name") or "").strip()]
            tagged_albums = [a for a in albums if (a.get("artist_name") or "").strip()]
            lines += [
                "",
                "Browse axes:",
                f"  artists:   {len(artists)} ({len(named_artists)} actually named)",
                f"  albums:    {len(albums)} ({len(tagged_albums)} with an artist tag)",
                f"  genres:    {len(genres)}",
                f"  playlists: {len(playlists)}"
                + (
                    "  -> " + ", ".join(
                        f"{p.get('name')} ({p.get('track_count', 0)} tracks)" for p in playlists
                    )
                    if playlists
                    else ""
                ),
            ]
            if len(named_artists) < len(artists) or not genres or not tagged_albums:
                lines += [
                    "",
                    "NOTE: the artist/album/genre axes are mostly EMPTY because these files "
                    "carry no embedded tags (a flat yt-dlp dump) — NOT because the library is "
                    "empty. There are real tracks here; the artist and song names live in the "
                    "TRACK TITLES. Use dj_search('<artist or song>') or dj_tracks(folder=...) "
                    "to see them, and don't rely on dj_queue_by(kind='artist'/'genre').",
                ]
            if not playlists or all(p.get("track_count", 0) == 0 for p in playlists):
                lines.append(
                    "NOTE: no non-empty playlists and no favorites exist yet, so "
                    "dj_queue_by(kind='playlist'/'favorites') has nothing to resolve."
                )
            lines += ["", "Next: dj_tracks(folder=...) to page the list, or dj_search('...') to find something."]
            return "\n".join(lines)

        if kind == "folders":
            listing = await _get(
                "/api/music/folders" + (f"?path={quote(path, safe='')}" if path else "")
            )
            lines = [f"Folder: {listing.get('path') or '(music roots)'}"]
            if listing.get("parent"):
                lines.append(f"Parent: {listing['parent']}")
            for f in listing.get("folders") or []:
                lines.append(
                    f"  [dir] {f.get('name')}  ({f.get('track_count')} tracks)  "
                    f"path={f.get('path')}"
                )
            for a in listing.get("albums") or []:
                lines.append(
                    f"  [album] {a.get('title')}  ({a.get('track_count')} tracks)  "
                    f"id={a.get('id')}"
                )
            if len(lines) == 1:
                lines.append("  (nothing here)")
            lines.append("Use dj_tracks(folder='<path>') to list the tracks.")
            return "\n".join(lines)

        if kind == "artists":
            artists = await _get("/api/music/artists")
            if not artists:
                return "No artists. Try dj_tracks/dj_search — the files may be untagged."
            lines = [f"{len(artists)} artist(s):"]
            for a in artists:
                name = (a.get("name") or "").strip()
                lines.append(
                    f"  {a.get('id')} | {name}" if name
                    else f"  {a.get('id')} | (NO NAME — untagged files; use dj_search instead)"
                )
            return "\n".join(lines)

        if kind == "albums":
            albums = await _get("/api/music/albums")
            if not albums:
                return "No albums in the library."
            lines = [f"{len(albums)} album(s):"]
            for a in albums:
                artist = (a.get("artist_name") or "").strip() or "unknown artist"
                lines.append(
                    f"  {a.get('id')} | {a.get('title')} — {artist} "
                    f"({a.get('track_count')} tracks)"
                )
            return "\n".join(lines)

        if kind == "genres":
            genres = await _get("/api/music/genres")
            if not genres:
                return (
                    "No genres — these files carry no genre tags. This does NOT mean the "
                    "library is empty; use dj_library() or dj_search() instead."
                )
            return f"{len(genres)} genre(s):\n" + "\n".join(
                f"  {g.get('name')} ({g.get('track_count', '?')} tracks)" for g in genres
            )

        if kind == "playlists":
            playlists = await _get("/api/playback/playlists")
            if not playlists:
                return "No playlists in the owner's library."
            return f"{len(playlists)} playlist(s):\n" + "\n".join(
                f"  {p.get('id')} | {p.get('name')} ({p.get('track_count', 0)} tracks)"
                for p in playlists
            )

        return (
            f"Unknown kind '{kind}'. Use 'overview', 'folders', 'artists', "
            "'albums', 'genres', or 'playlists'."
        )
    except PermissionError as e:
        return str(e)


@mcp.tool()
async def dj_tracks(
    folder: str | None = None,
    album: str | None = None,
    artist: str | None = None,
    playlist: str | None = None,
    offset: int = 0,
    limit: int = 50,
) -> str:
    """List actual tracks with their IDs, so you can choose what to play.

    Give exactly one of folder (path from dj_library), album, artist or
    playlist (names, matched loosely) — or none, to page the whole library.
    Paged via offset/limit; the footer tells you how to get the next page.

    Each line is `id | title | length`. Long items (podcasts, hours-long focus
    loops) are flagged LONG-FORM — never drop those into a music set.
    """
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    try:
        if folder:
            tracks = await _get(f"/api/music/folders/tracks?path={quote(folder, safe='')}")
            label = f"folder '{folder}'"
        elif album:
            albums = await _get("/api/music/albums")
            m = _best_match(albums, "title", album)
            if not m:
                return f"No album matching '{album}'."
            tracks = (await _get(f"/api/music/albums/{m['id']}")).get("tracks", [])
            label = f"album '{m['title']}'"
        elif artist:
            artists = await _get("/api/music/artists")
            m = _best_match(artists, "name", artist)
            if not m:
                return f"No artist matching '{artist}'. These files are largely untagged — try dj_search('{artist}')."
            tracks = await _get(f"/api/music/artists/{m['id']}/tracks")
            label = f"artist '{m['name'] or '(unnamed)'}'"
        elif playlist:
            playlists = await _get("/api/playback/playlists")
            m = _best_match(playlists, "name", playlist)
            if not m:
                return f"No playlist matching '{playlist}'."
            tracks = (await _get(f"/api/playback/playlists/{m['id']}")).get("tracks", [])
            label = f"playlist '{m['name']}'"
        else:
            tracks = await _all_music_tracks()
            label = "the whole music library"
    except PermissionError as e:
        return str(e)

    if not tracks:
        return f"{label} has no tracks."

    page = tracks[offset : offset + limit]
    if not page:
        return f"{label} has {len(tracks)} track(s); offset {offset} is past the end."

    shown_end = offset + len(page)
    lines = [f"{label} — {len(tracks)} track(s), showing {offset + 1}-{shown_end}:"]
    lines += [f"  {_track_line(t)}" for t in page]
    long_here = sum(1 for t in page if _is_longform(t))
    if long_here:
        lines.append(f"({long_here} flagged LONG-FORM above — keep them out of music sets.)")
    if shown_end < len(tracks):
        lines.append(f"More: repeat with offset={shown_end}.")
    lines.append("Pass any of these IDs to dj_play_now / dj_queue / dj_play_next.")
    return "\n".join(lines)


@mcp.tool()
async def dj_search(query: str, limit: int = 30, include_longform: bool = False) -> str:
    """Find tracks by name — the fastest way to turn an idea into track IDs.

    Matches every whitespace-separated term in `query` against the track title
    and artist (case-insensitive, in any order), so 'ashnikko daisy' works.
    Exact and prefix matches sort first.

    include_longform: False by default, which hides hours-long podcasts and
    focus loops so a music search returns music. Set True to find those on
    purpose (it reports how many it hid).
    """
    q = (query or "").strip()
    if not q:
        return "Give a search query — e.g. dj_search('ashnikko')."
    terms = q.lower().split()
    try:
        tracks = await _all_music_tracks()
    except PermissionError as e:
        return str(e)

    def haystack(t: dict) -> str:
        return f"{t.get('title') or ''} {t.get('artist_name') or ''}".lower()

    matches = [t for t in tracks if all(term in haystack(t) for term in terms)]
    hidden = 0
    if not include_longform:
        kept = [t for t in matches if not _is_longform(t)]
        hidden = len(matches) - len(kept)
        matches = kept

    if not matches:
        msg = f"No tracks matching '{q}'."
        if hidden:
            msg += f" ({hidden} long-form item(s) matched but were hidden — retry with include_longform=True.)"
        else:
            msg += " Try fewer or different words, or dj_library() to see what's there."
        return msg

    ql = q.lower()
    matches.sort(
        key=lambda t: (
            0 if _clean_title(t.get("title") or "").lower() == ql
            else 1 if haystack(t).strip().startswith(ql)
            else 2,
            (t.get("title") or "").lower(),
        )
    )
    page = matches[: max(1, limit)]
    lines = [f"{len(matches)} match(es) for '{q}'" + (f", showing {len(page)}" if len(page) < len(matches) else "") + ":"]
    lines += [f"  {_track_line(t)}" for t in page]
    if hidden:
        lines.append(f"({hidden} long-form item(s) hidden — include_longform=True to see them.)")
    lines.append("Pass these IDs to dj_play_now / dj_queue / dj_play_next.")
    return "\n".join(lines)


# --- Discovery + taste loop (item #2945, phase A) ---------------------------
#
# Lets the DJ propose music that ISN'T in the library yet and learn from how
# those proposals land. Three reasons this is MCP-side SQLite rather than a
# server table: a recommendation has no track row to hang off (play_stats.
# track_id is a FK to tracks, and the whole point is that the track isn't
# there yet); Favorite is binary with no room for a verdict or set context;
# and keeping it out of audiplex.db means no migration and no :8100 restart
# while Todd is listening.
#
# Feedback arrives VOICE-RELAYED through the agent — Todd says "yeah, that one
# was good" out loud and the agent calls dj_rate. There is deliberately no app
# UI for this yet; thumbs in the Android client cost a build, a versionCode
# bump and an in-app update round-trip, which is a lot to spend before we know
# the signal is worth anything.

TASTE_DB = Path(
    os.environ.get("DJ_TASTE_DB")
    or Path(__file__).resolve().parent.parent / "data" / "dj" / "taste.db"
)

# Feedback is dictated and relayed, so it arrives as whatever Todd actually
# said. Anything not recognised as praise is treated as 'meh' — the failure we
# care about is a lukewarm reaction being logged as a win.
_GOOD_WORDS = {
    "good", "great", "yes", "yeah", "yep", "love", "loved", "like", "liked",
    "nice", "banger", "keep", "more", "up", "1", "true",
}
_MEH_WORDS = {
    "meh", "no", "nope", "nah", "not", "bad", "skip", "pass", "down", "0",
    "false", "not-as-good", "notasgood", "worse",
}


@contextlib.contextmanager
def _taste_db():
    """Open (creating on first use) the taste store; commit and close on exit.

    Closing matters: this process is long-lived, and sqlite3's own connection
    context manager commits the transaction but leaves the handle open.
    """
    TASTE_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(TASTE_DB)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS recs (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at   TEXT NOT NULL,
            title        TEXT NOT NULL,
            artist       TEXT NOT NULL DEFAULT '',
            why          TEXT NOT NULL DEFAULT '',
            set_context  TEXT NOT NULL DEFAULT '',
            now_playing  TEXT NOT NULL DEFAULT '',
            verdict      TEXT,
            note         TEXT NOT NULL DEFAULT '',
            rated_at     TEXT
        )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS candidates (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at    TEXT NOT NULL,
            query         TEXT NOT NULL DEFAULT '',
            url           TEXT NOT NULL,
            title         TEXT NOT NULL DEFAULT '',
            artist        TEXT NOT NULL DEFAULT '',
            duration      REAL,
            rec_id        INTEGER,
            approval      TEXT NOT NULL DEFAULT '',
            ingested_at   TEXT,
            ingested_path TEXT NOT NULL DEFAULT ''
        )"""
        )
        yield conn
        conn.commit()
    finally:
        conn.close()


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _normalize_verdict(verdict: str) -> str:
    """'good' if it reads as praise, else 'meh'. Never guesses in favour of good."""
    words = re.findall(r"[a-z0-9]+", (verdict or "").lower())
    if any(w in _MEH_WORDS for w in words):
        return "meh"
    return "good" if any(w in _GOOD_WORDS for w in words) else "meh"


def _rec_label(row: sqlite3.Row) -> str:
    artist = (row["artist"] or "").strip()
    return f"{artist} - {row['title']}" if artist else row["title"]


async def _current_context() -> str:
    """What's playing right now, best-effort — the context a rec was made in."""
    try:
        state = await _get("/api/playback/state")
    except Exception:
        return ""
    track = state.get("track") or {}
    if not track.get("title"):
        return ""
    artist = (track.get("artist") or "").strip()
    title = _clean_title(track.get("title"))
    return f"{artist} - {title}" if artist else title


@mcp.tool()
async def dj_recommend(
    title: str,
    artist: str = "",
    why: str = "",
    set_context: str = "",
) -> str:
    """Propose a track that is NOT in the library yet, and log the proposal.

    Use this when you want to suggest something new — a track that would fit
    the set but that dj_search can't find because Audiplex doesn't have it.
    Logging it is what makes the suggestion learnable: dj_rate records how it
    landed and dj_taste feeds that back into your later picks.

    This QUEUES NOTHING and DOWNLOADS NOTHING. It records the idea and returns
    a short rec id to quote out loud ("that's rec 7") so Todd's reaction can be
    tied back to it.

    why: one line on why it fits — the reasoning is the part worth learning
         from, so say "same era as what's playing", not "good song".
    set_context: what you're going for right now (e.g. "late-night wind-down").
         What's actually playing is captured automatically.
    """
    name = (title or "").strip()
    if not name:
        return "Give a track title to recommend."
    playing = await _current_context()
    with _taste_db() as conn:
        cur = conn.execute(
            "INSERT INTO recs (created_at, title, artist, why, set_context, now_playing)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (_now(), name, (artist or "").strip(), (why or "").strip(),
             (set_context or "").strip(), playing),
        )
        rec_id = cur.lastrowid
    label = f"{artist.strip()} - {name}" if artist.strip() else name
    lines = [f"Logged rec {rec_id}: {label}"]
    if playing:
        lines.append(f"  (proposed over: {playing})")
    lines.append(
        f'Say the id out loud so it can be rated — then dj_rate({rec_id}, "good"|"meh"). '
        "Nothing was queued or downloaded."
    )
    return "\n".join(lines)


@mcp.tool()
async def dj_rate(rec_id: int = 0, verdict: str = "", note: str = "") -> str:
    """Record how a recommendation landed. This is the human half of the loop.

    rec_id: the id from dj_recommend. Leave it out (or pass 0) to rate the most
        recent UNRATED rec — which is the normal case, because Todd reacts to
        the thing you just suggested ("yeah, that one was good") without
        quoting a number.
    verdict: 'good' or 'meh'. Relay what he actually said; common phrasings are
        understood. Anything not clearly positive is recorded as 'meh' — a
        lukewarm reaction must not be banked as a win.
    note: his own words, if he gave a reason. This is the most useful column in
        the table — "too slow for a workout" teaches more than a bare 'meh'.
    """
    with _taste_db() as conn:
        if rec_id:
            row = conn.execute("SELECT * FROM recs WHERE id = ?", (rec_id,)).fetchone()
            if not row:
                return f"No rec {rec_id}. dj_taste() lists the recent ones."
        else:
            row = conn.execute(
                "SELECT * FROM recs WHERE verdict IS NULL ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if not row:
                return (
                    "No unrated recommendation to rate. Pass an explicit rec_id, "
                    "or dj_taste() to see what's been logged."
                )
        v = _normalize_verdict(verdict)
        # Keep the old note when this call doesn't carry one: correcting a
        # verdict must not wipe the reason he gave the first time, which is the
        # highest-signal thing in the table.
        new_note = (note or "").strip() or row["note"]
        conn.execute(
            "UPDATE recs SET verdict = ?, note = ?, rated_at = ? WHERE id = ?",
            (v, new_note, _now(), row["id"]),
        )
    was = f" (was already rated '{row['verdict']}')" if row["verdict"] else ""
    tail = f' — "{note.strip()}"' if (note or "").strip() else ""
    return (
        f"Rec {row['id']} ({_rec_label(row)}) rated {v}{tail}.{was}\n"
        "dj_taste() folds this into your next picks."
    )


@mcp.tool()
async def dj_taste(limit: int = 20) -> str:
    """Read back what's been learned about Todd's taste — check this BEFORE
    recommending, so picks improve instead of repeating.

    Three things: the verdicts on your past recommendations (with his own
    words, which carry the most signal), recs still awaiting a reaction, and
    the library-side play history.
    """
    limit = max(1, min(limit, 200))
    with _taste_db() as conn:
        rated = conn.execute(
            "SELECT * FROM recs WHERE verdict IS NOT NULL ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        unrated = conn.execute(
            "SELECT * FROM recs WHERE verdict IS NULL ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        totals = dict(
            conn.execute(
                "SELECT verdict, COUNT(*) FROM recs WHERE verdict IS NOT NULL"
                " GROUP BY verdict"
            ).fetchall()
        )

    lines: list[str] = []
    if not rated and not unrated:
        lines.append(
            "No recommendations logged yet — nothing learned. Use dj_recommend() "
            "when you suggest something that isn't in the library, then dj_rate() "
            "when Todd reacts."
        )
    else:
        lines.append(
            f"Rated recs: {totals.get('good', 0)} good / {totals.get('meh', 0)} meh."
        )
    for row in rated:
        bits = [f"  [{row['verdict']}] {row['id']}: {_rec_label(row)}"]
        if row["note"]:
            bits.append(f'      he said: "{row["note"]}"')
        if row["why"]:
            bits.append(f"      your reasoning: {row['why']}")
        ctx = row["set_context"] or row["now_playing"]
        if ctx:
            bits.append(f"      context: {ctx}")
        lines += bits
    if unrated:
        lines.append(f"Awaiting a reaction ({len(unrated)}) — ask about these:")
        lines += [f"  {row['id']}: {_rec_label(row)}" for row in unrated]

    # Todd's own stars (#3024) — the most direct taste signal there is, and
    # unlike the play-history reads below it is owner-scoped server-side, so
    # the DJ genuinely sees his ratings rather than its own empty account.
    try:
        stars = await _get("/api/playback/ratings")
    except Exception:
        stars = []
    if stars:
        lines.append(f"Todd's rated tracks ({len(stars)}) — dj_track_ratings() for the list:")
        for r in stars[:5]:
            bit = f"  [{'*' * r['rating']}] track {r['track_id']}"
            if r.get("note"):
                bit += f'  — "{r["note"]}"'
            lines.append(bit)

    # Library-side signal, read OWNER-scoped (#3028). These used to point at
    # /api/music/*, which filters by the CALLING user — and the DJ calls as
    # dj-agent, which has never played anything, so the lists were always empty
    # and the DJ would conclude Todd has no history rather than that it was
    # asking as the wrong account. The playback-router copies resolve
    # settings.dj_owner_username instead, the same fix /api/playback/ratings got.
    try:
        played = await _get("/api/playback/most-played?limit=10")
        skipped = await _get("/api/playback/likely-skips?limit=10")
    except Exception as e:
        lines.append(f"(Library play history unavailable: {e})")
        return "\n".join(lines)

    if played:
        lines.append("Most-played in the library:")
        lines += [f"  {_track_line(t)}" for t in played]
    if skipped:
        lines.append("Often skipped early — avoid these:")
        lines += [
            f"  {_track_line(t.get('track', t))}"
            f"  ({t.get('early_skip_count')} early skips of {t.get('total_starts')} starts)"
            for t in skipped
        ]
    if not played and not skipped:
        lines.append(
            "No library play history for the configured DJ owner. These reads "
            "are owner-scoped now (#3028), so unlike before this is a real "
            "answer and not an artefact of asking as dj-agent — the owner "
            "genuinely has no completed plays or early skips recorded yet. "
            "Still treat it as no data, not as dislike."
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Ingest (#2945 phase B) — find a track, get Todd's yes, download it TAGGED.
#
# Two tools on purpose. dj_find_candidates SEARCHES and downloads nothing;
# dj_ingest only accepts a candidate id that search produced, plus what Todd
# actually said when he approved it. So the DJ cannot go from "I like this
# song" to a file on disk without a candidate having been read out loud first,
# and every download carries an audit row saying who approved it and how.
#
# WHERE the file lands is the whole ballgame, and it is not where you'd guess.
# The music scanner is PATH-FIRST (server/audiplex/scanners/music.py): album
# title is the folder name and artist is the folder's PARENT — tags only supply
# per-track title and year. That is exactly why the existing dump is degenerate:
# 206 files sitting loose in q:\music make one album called "music" whose
# artist is the parent of the root, i.e. the empty string. Writing perfect tags
# on a file dropped in beside them would change NOTHING about the browse axes.
# So ingest builds <root>/<Artist>/<Album>/ (or the scanner's genre layout,
# <root>/Artists & Albums/<Genre>/<Artist>/<Album>/, when a genre is given) and
# writes the tags as well, since title/year still come from them.

FFMPEG_FALLBACK = r"C:\ProgramData\chocolatey\bin\ffmpeg.exe"
GENRE_PARENT = "Artists & Albums"

# Downloads land here first and are only moved into the library once they are
# a real, tagged .m4a. A half-written file inside a music root would otherwise
# be visible to the very rescan we fire at the end.
STAGING_DIR = TASTE_DB.parent / "staging"

_ARTIST_TITLE_SEP = re.compile(r"\s+[-–—]\s+")
_ILLEGAL_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _split_artist_title(raw_title: str, uploader: str = "") -> tuple[str, str]:
    """Best-effort "Artist - Title" split off a YouTube title.

    Falls back to the channel name with the " - Topic" suffix that YouTube's
    auto-generated artist channels carry stripped off.
    """
    cleaned = _clean_title(raw_title or "")
    parts = _ARTIST_TITLE_SEP.split(cleaned, maxsplit=1)
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        return parts[0].strip(), parts[1].strip()
    channel = re.sub(r"\s*-\s*Topic\s*$", "", uploader or "", flags=re.IGNORECASE)
    return channel.strip(), cleaned


def _plain(err: Exception) -> str:
    """yt-dlp colours its errors; ANSI escapes are noise in a relayed message."""
    return re.sub(r"\x1b\[[0-9;]*m", "", str(err)).strip()


def _safe_name(name: str, fallback: str = "Unknown") -> str:
    """A path segment Windows will actually accept."""
    cleaned = _ILLEGAL_FILENAME.sub("", name or "").strip().rstrip(". ")
    return cleaned[:120] or fallback


def _match_key(*parts: str) -> str:
    """Order-insensitive word key, for spotting a track we already have."""
    words = re.findall(r"[a-z0-9]+", _clean_title(" ".join(parts)).lower())
    return " ".join(sorted(words))


def _require(module: str, package: str):
    try:
        return __import__(module)
    except ImportError:
        raise RuntimeError(
            f"{module} is not installed for this interpreter "
            f"({sys.executable}). Install it with: pip install --user {package}"
        ) from None


def _search_youtube(query: str, limit: int) -> list[dict]:
    """Metadata-only search. Downloads nothing (`extract_flat` + no download)."""
    _require("yt_dlp", "yt-dlp")
    from yt_dlp import YoutubeDL

    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        # This process speaks JSON-RPC over stdout. yt-dlp's progress output
        # goes there by default and would corrupt the MCP transport mid-set,
        # so force every byte it emits onto stderr.
        "logtostderr": True,
        "noprogress": True,
        "skip_download": True,
        "extract_flat": True,
        "socket_timeout": 20,
    }
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
    return [e for e in (info or {}).get("entries") or [] if e]


def _download_audio(url: str, dest_dir: Path) -> tuple[Path, dict]:
    """Download `url` as audio into an EMPTY `dest_dir`; return (file, info)."""
    _require("yt_dlp", "yt-dlp")
    from yt_dlp import YoutubeDL

    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "logtostderr": True,  # keep yt-dlp off stdout — see _search_youtube
        "noprogress": True,
        "socket_timeout": 30,
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "outtmpl": str(dest_dir / "dl.%(ext)s"),
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "m4a", "preferredquality": "0"}
        ],
    }
    ffmpeg = shutil.which("ffmpeg") or (
        FFMPEG_FALLBACK if Path(FFMPEG_FALLBACK).exists() else None
    )
    if ffmpeg:
        opts["ffmpeg_location"] = ffmpeg

    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True) or {}

    produced = sorted(dest_dir.glob("dl.*"))
    m4a = [p for p in produced if p.suffix.lower() == ".m4a"]
    if not m4a:
        got = ", ".join(p.name for p in produced) or "nothing"
        raise RuntimeError(
            f"Download produced {got}, not an .m4a — ffmpeg conversion likely "
            "failed. Left in staging rather than putting an untaggable file in "
            "the library."
        )
    return m4a[0], info


def _write_tags(path: Path, title: str, artist: str, album: str, year=None) -> None:
    """Write the APPROVED metadata over whatever the source file carried.

    Deliberately not "keep the source tags if present": the existing library
    shows where that leads — Marillion's tracks all claim "Various Artists"
    and Barracuda carries no artist at all.
    """
    _require("mutagen", "mutagen")
    from mutagen.mp4 import MP4

    audio = MP4(str(path))
    audio["\xa9nam"] = [title]
    audio["\xa9ART"] = [artist]
    audio["aART"] = [artist]
    audio["\xa9alb"] = [album]
    if year:
        audio["\xa9day"] = [str(year)]
    audio.save()


async def _music_root() -> Path:
    roots = (await _get("/api/music/roots")).get("roots") or []
    usable = [r for r in roots if r.get("exists") and r.get("path")]
    if not usable:
        raise RuntimeError("No readable music root is configured on the server.")
    return Path(usable[0]["path"])


@mcp.tool()
async def dj_find_candidates(query: str, limit: int = 5, rec_id: int = 0) -> str:
    """Find real, downloadable sources for a track the library does NOT have —
    so a recommendation can become something Todd can actually play.

    THIS DOWNLOADS NOTHING. It searches and returns candidates with a candidate
    id each. Read the best one out to Todd; if he says yes, and only then, call
    dj_ingest with that id. That two-step is the approval gate — dj_ingest will
    not take a bare URL.

    query: what to search for — "artist track name" works best.
    rec_id: the dj_recommend id this came from, if any, so the taste loop and
        the download stay connected.
    """
    q = (query or "").strip()
    if not q:
        return "Give something to search for — 'artist track name'."
    limit = max(1, min(limit, 10))

    try:
        entries = await asyncio.to_thread(_search_youtube, q, limit)
    except Exception as e:
        return f"Search failed: {_plain(e)}"
    if not entries:
        return f"No results for '{q}'."

    try:
        library = await _all_music_tracks()
        have = {_match_key(t.get("title") or "") for t in library}
    except Exception:
        have = set()

    lines = [f"Candidates for '{q}' — nothing downloaded:"]
    with _taste_db() as conn:
        for e in entries:
            url = e.get("url") or e.get("webpage_url") or (
                f"https://www.youtube.com/watch?v={e['id']}" if e.get("id") else ""
            )
            if not url:
                continue
            raw = e.get("title") or "(untitled)"
            artist, title = _split_artist_title(
                raw, e.get("channel") or e.get("uploader") or ""
            )
            dur = e.get("duration")
            cur = conn.execute(
                "INSERT INTO candidates (created_at, query, url, title, artist,"
                " duration, rec_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (_now(), q, url, title, artist, dur, rec_id or None),
            )
            label = f"{artist} - {title}" if artist else title
            flags = []
            if isinstance(dur, (int, float)) and dur >= LONGFORM_SECONDS:
                flags.append("LONG-FORM — almost certainly not the song")
            if _match_key(artist, title) in have or _match_key(title) in have:
                flags.append("library already has something matching this")
            tail = f"  [{'; '.join(flags)}]" if flags else ""
            lines.append(f"  {cur.lastrowid} | {label} | {_fmt_duration(dur)}{tail}")

    lines.append(
        "Say the one you mean out loud and get Todd's yes, then "
        'dj_ingest(<id>, approval="<what he said>").'
    )
    return "\n".join(lines)


@mcp.tool()
async def dj_ingest(
    candidate_id: int,
    approval: str,
    artist: str = "",
    title: str = "",
    album: str = "",
    genre: str = "",
    allow_longform: bool = False,
    allow_duplicate: bool = False,
) -> str:
    """Download an APPROVED candidate into the library, properly tagged, and
    rescan so it's immediately playable.

    ONLY call this after Todd has said yes to a specific candidate from
    dj_find_candidates. `approval` is what he actually said — it is required,
    recorded, and it is the only evidence that a human authorised the download.
    Never fill it in on his behalf.

    artist/title/album/genre: override the search metadata when it's wrong —
        and check it, because YouTube titles are frequently wrong. These become
        the FOLDER LAYOUT as well as the tags, which is what makes the track
        browsable by artist instead of joining the untagged pile: with a genre
        it lands in `Artists & Albums/<Genre>/<Artist>/<Album>/`, without one in
        `<Artist>/<Album>/`. Album defaults to "Singles".
    allow_longform: required to ingest anything 15+ minutes — normally a sign
        the candidate is a mix or a full album upload, not the song.
    allow_duplicate: required if the library already looks to have this track.
    """
    said = (approval or "").strip()
    if not said:
        return (
            "Refusing: `approval` is empty. Ask Todd first and pass what he "
            "said — this tool downloads a file and adds it to his library."
        )
    with _taste_db() as conn:
        row = conn.execute(
            "SELECT * FROM candidates WHERE id = ?", (candidate_id,)
        ).fetchone()
    if not row:
        return (
            f"No candidate {candidate_id}. Run dj_find_candidates first and use "
            "an id it returned — this tool won't take a bare URL."
        )
    if row["ingested_at"]:
        return (
            f"Candidate {candidate_id} was already ingested on {row['ingested_at']}"
            f" → {row['ingested_path']}"
        )

    dur = row["duration"]
    if (
        not allow_longform
        and isinstance(dur, (int, float))
        and dur >= LONGFORM_SECONDS
    ):
        return (
            f"Refusing: that candidate is {_fmt_duration(dur)} — long enough to "
            "be a mix or a full-album upload rather than the track. Check it's "
            "really what you want, then pass allow_longform=True."
        )

    final_artist = (artist or row["artist"] or "").strip()
    final_title = (title or row["title"] or "").strip()
    if not final_title:
        return "Refusing: no title to tag this with. Pass title=..."
    if not final_artist:
        return (
            "Refusing: no artist. The artist is the folder name the scanner "
            "reads, so a blank one lands this in the untagged pile — the exact "
            "thing this is meant to stop. Pass artist=..."
        )
    final_album = (album or "").strip() or "Singles"

    if not allow_duplicate:
        try:
            have = {_match_key(t.get("title") or "") for t in await _all_music_tracks()}
        except Exception:
            have = set()
        if _match_key(final_artist, final_title) in have or _match_key(final_title) in have:
            return (
                f"Refusing: the library already looks to have '{final_artist} - "
                f"{final_title}'. dj_search to check; pass allow_duplicate=True "
                "if it really is a different recording."
            )

    try:
        root = await _music_root()
    except Exception as e:
        return f"Can't resolve the music root: {e}"

    parts = [_safe_name(final_artist), _safe_name(final_album)]
    if genre.strip():
        parts = [GENRE_PARENT, _safe_name(genre.strip())] + parts
    dest_dir = root.joinpath(*parts)
    dest = dest_dir / f"{_safe_name(final_title, 'track')}.m4a"
    if dest.exists():
        return f"Refusing: {dest} already exists. Nothing downloaded."

    staging = STAGING_DIR / f"c{candidate_id}"
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)

    try:
        downloaded, info = await asyncio.to_thread(_download_audio, row["url"], staging)
    except Exception as e:
        # Nothing here is worth keeping — partial fragments only. (Tagging and
        # move failures below deliberately DO keep the file.)
        shutil.rmtree(staging, ignore_errors=True)
        return f"Download failed, nothing added to the library: {_plain(e)}"

    # Duration is only trustworthy now — search gave a flat estimate, and the
    # long-form guard is worth nothing if the real file turns out to be an hour.
    real_dur = info.get("duration")
    if (
        not allow_longform
        and isinstance(real_dur, (int, float))
        and real_dur >= LONGFORM_SECONDS
    ):
        shutil.rmtree(staging, ignore_errors=True)
        return (
            f"Discarded: the downloaded file is {_fmt_duration(real_dur)}, not "
            "the short track the search suggested. Nothing was added. Pass "
            "allow_longform=True if you really want it."
        )

    try:
        await asyncio.to_thread(
            _write_tags, downloaded, final_title, final_artist, final_album,
            info.get("release_year"),
        )
    except Exception as e:
        return (
            f"Tagging failed: {e}. File left in {staging} and NOT added — an "
            "untagged file is what we're trying to stop shipping."
        )

    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(downloaded), str(dest))
    except OSError as e:
        return f"Could not move into the library: {e}. File is still in {staging}."
    shutil.rmtree(staging, ignore_errors=True)

    with _taste_db() as conn:
        conn.execute(
            "UPDATE candidates SET approval = ?, ingested_at = ?, ingested_path = ?"
            " WHERE id = ?",
            (said, _now(), str(dest), candidate_id),
        )

    lines = [
        f"Ingested: {final_artist} - {final_title}"
        f" ({_fmt_duration(real_dur or dur)})",
        f"  {dest}",
        f'  tagged artist/title/album, approved by Todd: "{said}"',
    ]

    # Music-roots-only rescan: this is the endpoint the DJ token is allowed to
    # call (#2947). Without it the file is on disk but invisible to the catalog.
    try:
        async with httpx.AsyncClient(timeout=180) as client:
            resp = await client.post(
                f"{AUDIPLEX_URL}/api/library/scan/music", headers=_headers()
            )
        resp.raise_for_status()
        scan = resp.json()
        lines.append(
            f"  rescan: added={scan.get('added')} updated={scan.get('updated')}"
            f" removed={scan.get('removed')}"
        )
        for err in (scan.get("errors") or [])[:3]:
            lines.append(f"  scan warning: {err}")
    except Exception as e:
        lines.append(
            f"  RESCAN FAILED ({e}) — the file is in place but the catalog "
            "hasn't picked it up, so it isn't playable yet."
        )
        return "\n".join(lines)

    found = []
    try:
        found = [
            t for t in await _all_music_tracks()
            if _match_key(t.get("title") or "") == _match_key(final_title)
        ]
    except Exception:
        pass
    if found:
        lines.append(f"  now playable: {_track_line(found[0])}")
    else:
        lines.append(
            "  NOTE: rescan ran but the track isn't showing in the catalog yet."
        )
    return "\n".join(lines)


# ----- DJ Pool & Specs: persistent mix configuration (#5470, #5473, #5477, #5495) -----
#
# #5495: the pool is owned by the Audiplex SERVER process (its bus hook tops it
# up on every track change). This MCP server is a different process, so every
# pool/spec tool goes over HTTP; importing the pool module here would mutate a
# private copy the server never sees. Source resolution stays here (it is the
# same catalog walk dj_mix uses); the server only ever receives track ids.


async def _resolve_lanes(sources: list[dict]) -> tuple[dict[str, list[int]], list[str]]:
    """Resolve spec sources into lanes {label: [track_ids]} plus the empty labels.

    A source may be {"kind": "tracks", "ids": [...]} (explicit ids, from
    dj_spec_add add_tracks). A no-match or an unknown folder path is an empty
    lane, never an exception; a 401 still raises PermissionError.
    """
    lanes: dict[str, list[int]] = {}
    empty: list[str] = []
    for src in sources:
        kind = str(src.get("kind", "folder"))
        query = str(src.get("query", ""))
        label = str(src.get("label") or query or kind)
        why = ""  # #2806: an unknown kind says which kinds exist, not just "0 tracks"
        if kind == "tracks" and src.get("ids"):
            ids = [int(i) for i in src.get("ids") or []]
        else:
            try:
                _, tracks = await _resolve_source(kind, query, recursive=bool(src.get("recursive", True)))
            except LookupError as e:  # #2806
                tracks, why = [], f" ({e})"
            except httpx.HTTPStatusError:
                tracks = []
            ids = [int(t["id"]) for t in tracks]
        lanes.setdefault(label, [])
        lanes[label].extend(i for i in ids if i not in lanes[label])
        if not ids:
            empty.append(label + why)  # #2806
    return lanes, empty


def _counts(lanes: dict[str, list[int]]) -> str:
    return ", ".join(f"{label}: {len(ids)}" for label, ids in lanes.items())


async def _get_spec(spec: str) -> dict | str:
    """The saved spec, or a sayable error string."""
    try:
        return await _get(f"/api/playback/mix-specs/{quote(spec, safe='')}")
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return f"ERROR: Spec '{spec}' not found"
        return f"ERROR reading spec '{spec}': {e}"


async def _pool_owns_spec(spec_id) -> bool:
    """Is the running pool the one started from this spec?"""
    try:
        status = await _get("/api/playback/pool")
    except Exception:
        return False
    return bool(status.get("active")) and status.get("spec_id") == spec_id


@mcp.tool()
async def dj_spec_save(
    name: str,
    request_text: str,
    sources: list[dict],
    balance: str = "even",
    ahead: int = 4,
    exclude_recent_hours: float = 12,
    allow_empty: bool = False,
) -> str:
    """Save a DJ mix spec on the server.

    Resolves each source and returns per-source track counts. Refuses (returns
    error, no save) if any named source resolves to 0 tracks unless
    allow_empty=True (#5473).

    name:                 unique spec name
    request_text:         Todd's request verbatim (for reference)
    sources:              [{kind, query, recursive?, label}, ...]
    balance:              "even", "proportional", or "none"
    ahead:                how many tracks to keep queued (default 4)
    exclude_recent_hours: hours to exclude recently-played tracks (default 12)
    allow_empty:          allow sources that resolve to 0 tracks
    """
    try:
        lanes, empty = await _resolve_lanes(sources)
    except PermissionError as e:
        return str(e)
    if empty and not allow_empty:
        return f"REFUSED: empty source(s): {', '.join(empty)}. Nothing saved."
    saved = await _post("/api/playback/mix-specs", {
        "name": name,
        # #5530: the seed spec keeps Todd's verbatim messages as a list; the
        # TEXT column needs a string, so structured request text goes as JSON.
        "request_text": request_text if isinstance(request_text, str)
        else json.dumps(request_text, ensure_ascii=False),
        "sources": sources,
        "balance": balance,
        "ahead": ahead,
        "exclude_recent_hours": exclude_recent_hours,
        "last_counts": {label: len(ids) for label, ids in lanes.items()},
    })
    return f"Saved spec '{name}' (id {saved.get('id')}): {_counts(lanes)}"


async def _trim_to_pool(picks: list[int], state: dict) -> str:
    """One-time queue trim when a pool starts (#5495): the pool's first picks
    replace whatever was queued after the current song. Same delivery as
    dj_mix: replace_upcoming, or on an old phone build a play_now swap at the
    next song boundary (never mid-song, never while paused)."""
    track = state.get("track") or {}
    if track.get("id") is None or not state.get("queue"):
        data = await _enqueue("play_now", {"track_ids": picks})
        if isinstance(data, str):
            return " " + data
        result = await _result(data, len(picks))  # #2843 (Jarvis rider 2): a verdict, not "starts now"
        return (f"\n{result}Nothing was loaded, so the pool's first picks were sent to start "
                f"now (command #{data.get('id')}).")
    if not await _device_lacks_replace_upcoming():
        data = await _enqueue("replace_upcoming", {"track_ids": picks})
        if isinstance(data, str):
            return " " + data
        ack = await _await_ack(int(data["id"]))
        if ack is None:
            return (f" Sent replace_upcoming (command #{data['id']}) after the current song;"
                    " no ack yet - check dj_command_status.")
        if ack.get("ack_status") == "ok":
            return f" The queue after the current song is now the pool's (command #{data['id']})."
        if ack.get("ack_status") != "unknown_type":
            return f" The phone refused the trim: {ack.get('ack_status')} {ack.get('ack_detail') or ''}".rstrip()
    old = _SWAP.get("task")
    if old is not None and not old.done():
        old.cancel()
    if not state.get("playing"):
        return (" Old phone build and the player is paused: swapping the queue would start"
                " music, so NOTHING was sent; the old queue plays first.")
    rem = _remaining_ms(state)
    wait = max(SWAP_MIN_WAIT_S, (rem or 0) / 1000 + 60)
    _SWAP["ids"] = list(picks)
    _SWAP["task"] = asyncio.create_task(_boundary_swap(list(picks), int(track["id"]), wait))
    _swap_set("pending", f"{len(picks)} pool track(s) replace the queue when the current song ends")
    return " Old phone build: the pool's picks replace the queue when the current song ends (dj_mix_status)."


@mcp.tool()
async def dj_pool_set(
    spec: str | None = None,
    spec_id: int | None = None,
    sources: list[dict] | None = None,
    balance: str | None = None,
    ahead: int | None = None,
    exclude_recent_hours: float | None = None,
    allow_empty: bool = False,
    starvation_picks: int | None = None,
    starvation_minutes: float | None = None,
) -> str:
    """Start (or replace) the rolling DJ pool from a saved spec or inline sources.

    Once running, the SERVER keeps `ahead` tracks queued after the current song,
    appending on every track change: round-robin across lanes (one lane per
    source) with a starvation rule, skipping recently-played recordings and
    anything already queued. Starting it replaces what was queued after the
    current song with the pool's first picks, once.

    spec / spec_id:       a saved spec (by name or id); its cues are armed too
    sources:              inline sources if no spec: [{kind, query, recursive?, label}]
    balance:              "even", "proportional", or "none" (spec value, else even)
    ahead:                how many tracks to keep queued (spec value, else 4)
    exclude_recent_hours: skip recently-played recordings (spec value, else 12)
    allow_empty:          a source that resolves to 0 tracks REFUSES unless True
    starvation_picks / starvation_minutes: a lane unpicked this long jumps the line
    """
    try:
        saved: dict = {}
        if spec is None and spec_id is not None:
            match = [s for s in await _get("/api/playback/mix-specs") if s.get("id") == spec_id]
            if not match:
                return f"ERROR: no saved spec with id {spec_id}"
            spec = match[0]["name"]
        if spec is not None:
            got = await _get_spec(spec)
            if isinstance(got, str):
                return got
            saved = got
            spec_id = saved.get("id")
            sources = saved.get("sources") or []
        if not sources:
            return "ERROR: no sources given and no spec named."
        balance = balance or saved.get("balance") or "even"
        ahead = int(ahead if ahead is not None else saved.get("ahead") or 4)
        if exclude_recent_hours is None:
            exclude_recent_hours = saved.get("exclude_recent_hours", 12)

        lanes, empty = await _resolve_lanes(sources)
        if empty and not allow_empty:
            return (f"REFUSED, pool not started: empty source(s): {', '.join(empty)}. "
                    f"Per lane: {_counts(lanes)}. Fix the source or pass allow_empty=True.")

        state = await _get("/api/playback/state")
        track = state.get("track") or {}
        cur = track.get("id")
        queue = state.get("queue") or []
        if cur is None or not queue:  # #3493: this pool would START music, so refuse before saving it
            muted = _mute_hold("play_now")
            if muted:
                return _held_result(muted)
        body: dict = {
            "spec_id": spec_id,
            "lanes": lanes,
            "balance": balance,
            "ahead": ahead,
            "exclude_recent_hours": exclude_recent_hours,
            "queued_ids": [q.get("id") for q in queue if isinstance(q.get("id"), int)],
        }
        starve = {}
        if starvation_picks is not None:
            starve["check_interval_picks"] = int(starvation_picks)
        if starvation_minutes is not None:
            starve["check_interval_minutes"] = float(starvation_minutes)
        if starve:
            body["starvation_config"] = starve
        if cur is None or not queue:
            body["prime_current_id"] = 0
        elif cur > 0:
            body["prime_current_id"] = cur
        result = await _post("/api/playback/pool", body)
    except PermissionError as e:
        return str(e)

    head = (f"Pool set: {len(lanes)} lane(s) ({_counts(lanes)}), balance={balance}, "
            f"ahead={ahead}" + (f", spec '{spec}'" if spec else "") + ".")
    if "prime_current_id" not in body:
        return head + " A live stream or DJ break is on; the pool fills in after it."
    picks = result.get("initial_picks") or []
    if not picks:
        return head + " No eligible picks right now (everything recent or already queued)."
    return head + await _trim_to_pool(picks, state)


@mcp.tool()
async def dj_pool_status() -> str:
    """Get current DJ pool status: per-lane details and pending cues."""
    status = await _get("/api/playback/pool")
    if not status.get("active"):
        return "Pool is not running"

    lines = [
        f"Pool status (spec {status.get('spec_id')}):",
        f"  Balance: {status.get('balance_mode')}, Ahead: {status.get('ahead')}",
        f"  Total tracks: {status.get('eligible_count')}, Played this session: {status.get('played_this_session_count')}",
        "Lanes:",
    ]
    for lane in status.get("lanes", []):
        exhausted_mark = " [EXHAUSTED]" if lane.get("exhausted") else ""
        last_played = f" (last {lane.get('minutes_since_played')}m ago)" if lane.get("minutes_since_played") else ""
        lines.append(
            f"  {lane['label']:20} {lane['remaining']:3} remaining, "
            f"{lane['played_count']:3} played{exhausted_mark}{last_played}"
        )
    cues = status.get("cues") or status.get("pending_cues") or []
    if cues:
        lines.append(f"Pending cues: {len(cues)}")
        for cue in cues[:6]:
            trig = cue.get("trigger") or {}
            held = f", held {cue.get('held_boundaries')}x" if cue.get("held_boundaries") else ""
            lines.append(
                f"  Cue #{cue.get('id')} [{cue.get('status', 'pending')}{held}] "
                f"{trig.get('kind')} {trig.get('track_id') or ''}: {cue.get('say') or 'n/a'}"
            )
    chimes = status.get("chimes") or {}
    if chimes:
        if status.get("chimes_unsupported"):
            state = "UNSUPPORTED by the phone app (disabled this session)"
        else:
            state = "on" if chimes.get("enabled") else "off"
        strikes = "on" if chimes.get("hour_strikes") else "off"
        last = f", last: {chimes.get('last_result')}" if chimes.get("last_result") else ""
        lines.append(f"Chimes: {state}, volume {chimes.get('volume')}, hour strikes {strikes}{last}")
    outro = status.get("outro")
    if outro:
        pause = f", pause {outro.get('pause_state')}" if outro.get("pause_state") else ""
        said = f": {outro.get('say')}" if outro.get("say") else ""
        lines.append(f"Outro armed: after track {outro.get('track_id')}, {outro.get('status')}{pause}{said}")
    return "\n".join(lines)


@mcp.tool()
async def dj_chimes(
    enabled: bool | None = None, volume: float | None = None, hour_strikes: bool | None = None
) -> str:
    """Westminster quarter chimes during a DJ pool (#5499): on/off, volume 0-1
    (default 0.12, under the music), and whether :00 also strikes the hour.
    Chimes ring at :15 :30 :45 :00 on the phone's bed layer only while a pool
    is running and music is playing; never while Todd is talking."""
    body = {k: v for k, v in
            {"enabled": enabled, "volume": volume, "hour_strikes": hour_strikes}.items()
            if v is not None}
    try:
        s = await _patch("/api/playback/pool/chimes", body)
    except PermissionError as e:
        return str(e)
    state = "on" if s.get("enabled") else "off"
    strikes = "on" if s.get("hour_strikes") else "off"
    return f"Chimes {state}, volume {s.get('volume')}, hour strikes {strikes}."


@mcp.tool()
async def dj_pool_stop() -> str:
    """Stop the rolling DJ pool (what is already queued keeps playing)."""
    result = await _delete("/api/playback/pool")
    if result.get("stopped"):
        return "Stopped the rolling pool."
    return "No rolling pool was running."


# ----- DJ Mix Specs: create/manage persistent specs (#5477) -----


@mcp.tool()
async def dj_spec_add(
    spec: str,
    add_sources: list[dict] | None = None,
    add_tracks: list[int] | None = None,
) -> str:
    """Add sources or specific tracks to a saved mix spec; re-syncs the pool if it is running this spec.

    Refuses (nothing saved) if any new source resolves to 0 tracks.

    add_sources: [{kind, query, recursive?, label}, ...]
    add_tracks:  [track_id, ...] (stored as one "added tracks" source)
    """
    new = list(add_sources or [])
    if add_tracks:
        new.append({"kind": "tracks", "ids": [int(i) for i in add_tracks], "label": "added tracks"})
    if not new:
        return "Nothing to add."
    saved = await _get_spec(spec)
    if isinstance(saved, str):
        return saved
    try:
        new_lanes, empty = await _resolve_lanes(new)
        if empty:
            return f"REFUSED, spec unchanged: empty source(s): {', '.join(empty)}."
        body: dict = {"add_sources": new}
        if await _pool_owns_spec(saved.get("id")):
            lanes, _ = await _resolve_lanes(saved.get("sources") or [])
            for label, ids in new_lanes.items():
                lanes.setdefault(label, []).extend(i for i in ids if i not in lanes[label])
            body["lanes"] = lanes
        result = await _patch(f"/api/playback/mix-specs/{quote(spec, safe='')}", body)
    except PermissionError as e:
        return str(e)
    synced = " (pool re-synced)" if result.get("pool_resynced") else ""
    return f"Added to spec '{spec}': {_counts(new_lanes)}{synced}"


@mcp.tool()
async def dj_spec_remove(spec: str, remove_sources: list[str] | None = None) -> str:
    """Remove sources (by query or label) from a saved mix spec; re-syncs the pool if it is running this spec."""
    if not remove_sources:
        return "Nothing to remove."
    saved = await _get_spec(spec)
    if isinstance(saved, str):
        return saved
    gone = set(remove_sources)
    before = saved.get("sources") or []
    remaining = [s for s in before if s.get("query") not in gone and s.get("label") not in gone]
    try:
        body: dict = {"remove_sources": list(remove_sources)}
        if await _pool_owns_spec(saved.get("id")):
            body["lanes"], _ = await _resolve_lanes(remaining)
        result = await _patch(f"/api/playback/mix-specs/{quote(spec, safe='')}", body)
    except PermissionError as e:
        return str(e)
    synced = " (pool re-synced)" if result.get("pool_resynced") else ""
    return f"Removed {len(before) - len(remaining)} source(s) from spec '{spec}'{synced}"


@mcp.tool()
async def dj_spec_note(
    spec: str,
    after_track_id: int | None = None,
    play_track_id: int | None = None,
    say: str | None = None,
    trigger_kind: str = "track_end",
    clip_id: str | None = None,
    agent: str | None = None,
) -> str:
    """Add a cue/note to a mix spec.

    Cues fire when a trigger matches and can play a track, speak patter, or
    both. trigger_kind 'track_end' + after_track_id: the patter plays right
    after that song ends; 'track_start': right before that song starts.
    say= is rendered to audio NOW in the DJ voice (the same synthesis
    dj_announce uses) and stored as a clip on the cue, so nothing is spoken
    live when it fires. A cue is held while Todd is talking and dropped if he
    spoke after it was rendered. agent: who wrote the patter (shown on the
    clip title, e.g. "DJ break · Jarvis"); defaults to DJ_PERSONA_NAME.
    If the running pool is this spec's, the cue is armed on it immediately.
    """
    body: dict = {
        "after_track_id": after_track_id,
        "play_track_id": play_track_id,
        "say": say,
        "trigger_kind": trigger_kind,
        "clip_id": clip_id,
    }
    say_text = (say or "").strip()
    if say_text and not clip_id:  # #5480: render ahead of time, never live at fire time
        who = agent or dj_persona.persona_name()
        title = f"DJ break · {who}"
        clip = await _render_clip(say_text, title)
        if isinstance(clip, str):
            return f"ERROR: cue not added, the patter could not be rendered: {clip}"
        body.update(
            clip_id=clip["clip_id"],
            clip_title=title,
            clip_duration=clip.get("duration_seconds"),
            rendered_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            voice=tts_backend.voice(),
            agent=who,
        )
    try:
        cue = await _post(f"/api/playback/mix-specs/{quote(spec, safe='')}/notes", body)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return f"ERROR: Spec '{spec}' not found"
        raise
    rendered = f" (patter pre-rendered as clip #{body['clip_id']})" if body.get("clip_title") else ""
    return f"Added cue to spec '{spec}': {cue.get('id')}{rendered}"


@mcp.tool()
async def dj_spec_notes(spec: str) -> str:
    """List all cues/notes for a mix spec."""
    try:
        notes = (await _get(f"/api/playback/mix-specs/{quote(spec, safe='')}/notes")).get("notes") or []
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return f"ERROR: Spec '{spec}' not found"
        raise
    if not notes:
        return f"Spec '{spec}' has no cues"
    summary = [
        f"  Cue #{n['id']}: after track {(n.get('trigger') or {}).get('track_id')}, say '{n.get('say') or ''}'"
        for n in notes
    ]
    return f"Spec '{spec}' cues:\n" + "\n".join(summary)


@mcp.tool()
async def dj_spec_list() -> str:
    """List all saved mix specs."""
    specs = await _get("/api/playback/mix-specs")
    if not specs:
        return "No mix specs saved"
    summary = [
        f"  {s['name']:20} | id={s.get('id')} balance={s.get('balance'):12} ahead={s.get('ahead')} | {(s.get('request_text') or '')[:50]}"
        for s in specs
    ]
    return f"Mix specs ({len(specs)}):\n" + "\n".join(summary)


@mcp.tool()
async def dj_spec_show(spec: str) -> str:
    """Show a specific mix spec's configuration and cues."""
    saved = await _get_spec(spec)
    if isinstance(saved, str):
        return saved
    sources = saved.get("sources") or []
    notes = saved.get("notes") or []
    lines = [
        f"Spec: {saved.get('name')} (id {saved.get('id')})",
        f"Request: {saved.get('request_text')}",
        f"Balance: {saved.get('balance')}, Ahead: {saved.get('ahead')}, Exclude recent: {saved.get('exclude_recent_hours')}h",
        f"Sources: {len(sources)}",
    ]
    for src in sources:
        kind = src.get("kind", "folder")
        query = src.get("query", "") if kind != "tracks" else f"{len(src.get('ids') or [])} track id(s)"
        label = src.get("label") or query
        lines.append(f"  - {kind:8} {label:20} ({query})")
    if notes:
        lines.append(f"Cues: {len(notes)}")
        for cue in notes:
            track_id = (cue.get("trigger") or {}).get("track_id")
            play = cue.get("play_track")
            lines.append(f"  - After track {track_id}: say '{cue.get('say') or ''}'" + (f", play {play}" if play else ""))
    return "\n".join(lines)


# #5448: dj_fetch_from_playlists lives in its own module (server runs as __main__).
from audiplex_mcp import playlist_fetch  # noqa: E402  #5448

playlist_fetch.register(mcp, globals())  # #5448

from audiplex_mcp import bucket_tools  # noqa: E402  #5518: themed music buckets

bucket_tools.register(mcp, globals())  # #5518

from audiplex_mcp import dj_toolkit  # noqa: E402  #2806: queue edits, pool lanes, bans

dj_toolkit.register(mcp, globals())  # #2806


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
