"""Audiplex DJ — full-ride rehearsal against a COPY of the live library (#ride0928).

A persona runs a whole bike ride through the real MCP tools while a simulated
phone plays it back. Unlike dj_e2e_harness.py (6 seeded tracks), this runs on
a snapshot of the live audiplex.db and config.yaml, so real folders, the real
todd-ride-mix spec and real files on disk are what get resolved and streamed.

What it proves, per ride:
  1. bike start is HELD while Todd is talking, then starts once he's done
     (after a persona announced it in the temp chat DB)
  2. the rolling pool starts from todd-ride-mix, and the phone's range GET on
     every track it's handed returns audio bytes (the files really exist)
  3. a spec cue fires at a song boundary (pre-rendered clip inserted)
  4. dj_break_brief names Previous / Now playing / Next
  5. dj_mix on the spec's sources; dj_folder shuffles a folder
  6. play_now with a dead-path track: skipped and named in RESULT; a dead file
     that reaches the phone anyway is skipped there and shows as missing_on_phone
  7. a quarter-hour chime while Todd talks is dropped (engine, in-process)
  8. dj_resume while Todd talks is HELD
  9. dj_outro arms; the song ends; the outro clip plays; the player pauses

Safety: refuses port 8100 and refuses a DB path that resolves to the live DB.
Everything it writes lives in a temp dir (DB copy, config copy, pool state,
logs, speech state, chat DB, clips). The live server is never contacted.

Run:  cd server && C:\\Python311\\python.exe tests/ride_rehearsal.py [--repeat 3] [--keep]
Exits 0 only if every check in every repeat passed.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import yaml

SERVER_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SERVER_DIR.parent
for p in (str(SERVER_DIR), str(REPO_ROOT), str(Path(__file__).resolve().parent)):
    if p not in sys.path:
        sys.path.insert(0, p)

from dj_e2e_harness import (  # noqa: E402
    FORBIDDEN_PORTS, FakeTts, Report, SimulatedDevice, free_port, start_server, wait_ready,
)

LIVE_DB = (SERVER_DIR / "audiplex.db").resolve()
LIVE_CONFIG = SERVER_DIR / "config.yaml"
SPEC = "todd-ride-mix"
DEAD_PATH = "Z:/ride-rehearsal/no-such-drive/dead-track.mp3"


# ------------------------------------------------------------------ fixture

def snapshot(tmp: Path, port: int) -> dict:
    """Copy the live DB (sqlite backup API, safe while :8100 writes) + config."""
    db = tmp / "audiplex.db"
    if db.resolve() == LIVE_DB:
        raise SystemExit("REFUSED: rehearsal DB path resolves to the live DB")
    src = sqlite3.connect(f"file:{LIVE_DB.as_posix()}?mode=ro", uri=True)
    dst = sqlite3.connect(db)
    src.backup(dst)
    src.close()

    # Inject a track whose file can't exist (a drive letter that isn't there).
    row = dst.execute("SELECT album_id, artist_id FROM tracks LIMIT 1").fetchone()
    dst.execute("DELETE FROM tracks WHERE file_path = ?", (DEAD_PATH,))
    cur = dst.execute(
        "INSERT INTO tracks (title, album_id, artist_id, disc_number, track_number, duration_seconds,"
        " file_path, file_size, added_at, updated_at) VALUES (?,?,?,1,1,200,?,1,?,?)",
        ("Rehearsal Dead Track", row[0], row[1], DEAD_PATH, datetime.utcnow(), datetime.utcnow()),
    )
    dead_id = cur.lastrowid
    agent = dst.execute("SELECT id, username FROM users WHERE username = 'dj-agent'").fetchone()
    dst.commit()
    dst.close()

    cfg = yaml.safe_load(LIVE_CONFIG.read_text(encoding="utf-8"))
    cfg.update({
        "database_url": f"sqlite:///{db.as_posix()}",
        "port": port,
        "host": "127.0.0.1",
        "scan_on_startup": False,
        "cover_cache_dir": (tmp / "covers").as_posix(),
        "dj_clip_dir": (tmp / "dj_clips").as_posix(),
    })
    if not cfg.get("jwt_secret"):
        raise SystemExit("live config has no jwt_secret; refusing to let the copy mint one")
    (tmp / "config.yaml").write_text(yaml.safe_dump(cfg, default_flow_style=False), encoding="utf-8")

    from audiplex.auth import create_token
    token = create_token(agent[0], agent[1], cfg["jwt_secret"], 24)
    return {"db": db, "token": token, "dead_id": dead_id}


def isolate_env(tmp: Path, base: str, tts_url: str, token: str) -> None:
    """Point EVERY file-backed setting at the temp dir, before server + MCP start."""
    env = {
        "AUDIPLEX_DJ_POOL_STATE": tmp / "dj_pool.json",
        "AUDIPLEX_EXIT_LOG": tmp / "client-exits.jsonl",
        "AUDIPLEX_LINK_LOG": tmp / "link-history.jsonl",
        "AUDIPLEX_CHIME_CACHE_DIR": tmp / "chimes",
        "DJ_SPEECH_STATE_FILE": tmp / "speech_state.json",
        "DJ_PANTHEON_DB": tmp / "pantheon.db",
        "DJ_BUCKETS_DB": tmp / "buckets.db",
        "DJ_MIX_SOURCES_FILE": tmp / "dj_mix_sources.json",
        "DJ_TASTE_DB": tmp / "taste.db",
        "DJ_PATTER_FILE": tmp / "dj_patter.json",
        "DJ_BRIDGE_LOG": tmp / "dj_bridge.log",
        "DJ_YT_PLAYLIST_CACHE": tmp / "yt_cache",
        "AUDIPLEX_DB": tmp / "audiplex.db",
    }
    for k, v in env.items():
        os.environ[k] = str(v)
    os.environ.update({
        "AUDIPLEX_PUBLIC_URL": base, "AUDIPLEX_URL": base, "AUDIPLEX_TOKEN": token,
        "DJ_TTS_URL": tts_url, "DJ_TTS_FORMAT": "wav", "DJ_PERSONA_NAME": "the DJ",
    })
    for k in ("DJ_TTS_CMD", "DJ_LAT", "DJ_LON"):
        os.environ.pop(k, None)
    con = sqlite3.connect(tmp / "pantheon.db")
    con.execute("CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY, sender TEXT,"
                " content TEXT, timestamp TEXT)")
    con.commit()
    con.close()


def talk(tmp: Path, talking: bool) -> None:
    spoke = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    (tmp / "speech_state.json").write_text(json.dumps({
        "stt_active": talking, "talk_active": False, "composing": False,
        "current_claim_holder": None, "todd_last_spoke_at": spoke,
    }), encoding="utf-8")


def announce_in_chat(tmp: Path, text: str) -> None:
    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
    con = sqlite3.connect(tmp / "pantheon.db")
    con.execute("INSERT INTO messages (sender, content, timestamp) VALUES ('claude', ?, ?)", (text, ts))
    con.commit()
    con.close()


# ------------------------------------------------------------ the phone

class RidePhone(SimulatedDevice):
    """SimulatedDevice that behaves like the #ride0928 phone: resolves each id,
    range-GETs its stream (a file that isn't there is dropped and named in the
    ack), acks every command, and skips past a file that fails mid-queue."""

    def __init__(self, base: str, token: str) -> None:
        super().__init__(base, token)
        self.streamed: dict[int, int] = {}   # track id -> bytes read by the range GET
        self.unplayable: list[int] = []
        self.acks: list[tuple] = []
        self.ack_errors: list[tuple] = []
        self.done_ids: set[int] = set()   # the real client dedupes redeliveries on id

    async def load_catalog(self, client: httpx.AsyncClient) -> None:
        return None  # 5000+ tracks: resolve lazily per command instead

    async def _resolve(self, client: httpx.AsyncClient, ids: list[int]) -> tuple[list[dict], list[int]]:
        """Resolve 8 at a time, like the #3249 phone fix, keeping order."""
        gate = asyncio.Semaphore(8)

        async def one(i: int):
            async with gate:
                r = await client.get(f"{self.base}/api/music/tracks/{i}", headers=self.headers)
                s = await client.get(f"{self.base}/api/music/stream/track/{i}",
                                     headers={**self.headers, "Range": "bytes=0-1023"})
            if r.status_code != 200 or s.status_code not in (200, 206) or not s.content:
                return i, None, 0
            t = r.json()
            return i, {"id": i, "title": t.get("title"), "artist": t.get("artist_name"),
                       "duration_ms": int((t.get("duration_seconds") or 0) * 1000)}, len(s.content)

        items, dropped = [], []
        for i, item, n in await asyncio.gather(*(one(i) for i in ids if i > 0)):
            if item is None:
                dropped.append(i)
                self.unplayable.append(i)
            else:
                self.streamed[i] = n
                items.append(item)
        return items, dropped

    async def dispatch(self, client: httpx.AsyncClient, cmd: dict) -> None:
        ctype, payload = cmd["type"], cmd.get("payload") or {}
        status, detail = "ok", ""
        t0 = time.time()
        if ctype in ("play_now", "queue", "play_next", "replace_upcoming"):
            items, dropped = await self._resolve(client, list(payload.get("track_ids") or []))
            if dropped:
                detail = f"dropped {len(dropped)} unresolvable id(s): {dropped}"
            if not items:
                status = "failed"
            elif ctype == "replace_upcoming":
                self.queue = self.queue[: self.index + 1] + items
                self.seen.append(ctype)
            else:
                self.catalog.update({it["id"]: it for it in items})
                self.apply(ctype, {"track_ids": [it["id"] for it in items]})
        elif ctype in ("bed_play", "bed_stop", "bed_volume"):
            self.seen.append(ctype)
        else:
            self.apply(ctype, payload)
            if self.seen and self.seen[-1].startswith("UNKNOWN:"):
                status = "unknown_type"
        ack = {"status": status, **({"detail": detail} if detail else {})}
        r = await client.post(f"{self.base}/api/playback/command/{cmd['id']}/ack",
                              headers=self.headers, json=ack)
        if r.status_code != 200:
            self.ack_errors.append((cmd["id"], r.status_code, r.text[:120]))
        self.acks.append((cmd["id"], ctype, status, round(time.time() - t0, 2),
                          round(time.time() - (cmd.get("created_at") or t0), 2)))

    async def run(self) -> None:
        async with httpx.AsyncClient(timeout=40) as client:
            await self._report(client)
            while not self._stop:
                try:
                    r = await client.get(f"{self.base}/api/playback/command/next", headers=self.headers)
                    if r.status_code == 204:
                        continue
                    r.raise_for_status()
                    cmd = r.json()
                    if cmd["id"] in self.done_ids:
                        continue
                    self.done_ids.add(cmd["id"])
                    await self.dispatch(client, cmd)
                    await self._report(client)
                    self.applied += 1
                except httpx.ReadTimeout:
                    continue
                except Exception:
                    if self._stop:
                        return
                    await asyncio.sleep(0.2)

    async def song_ends(self) -> None:
        """The current song finishes: advance like ExoPlayer, report the boundary."""
        async with httpx.AsyncClient(timeout=20) as client:
            cur = self.queue[self.index] if 0 <= self.index < len(self.queue) else None
            if cur and cur.get("duration_ms"):
                self.position_ms = cur["duration_ms"] - 500
                await self._report(client)
            if self.index + 1 < len(self.queue):
                self.index += 1
                self.position_ms = 0
            else:
                self.playing = False
            await self._report(client)

    async def hit_dead_file(self, track_id: int) -> None:
        """A file dies under the player (drive unplugged mid-ride): what the
        #ride0928 PlaybackManager does — skip to the next item, log skipped=true."""
        async with httpx.AsyncClient(timeout=20) as client:
            skipped = self.index + 1 < len(self.queue)
            await client.post(f"{self.base}/api/playback/client-log", headers=self.headers, json={
                "level": "error", "event": "player_error", "message": "Source error",
                "at": time.time(),
                "detail": {"trackId": str(track_id), "trackTitle": "Rehearsal Dead Track",
                           "causeMessage": "Response code: 404", "skipped": str(skipped).lower()},
            })
            if skipped:
                self.index += 1
                self.position_ms = 0
            await self._report(client)


async def seen_after(dev: RidePhone, mark: int, ctype: str, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if ctype in dev.seen[mark:]:
            await asyncio.sleep(0.3)
            return True
        await asyncio.sleep(0.1)
    return False


async def settle(dev: RidePhone, start: int, n: int = 1, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if dev.applied >= start + n:
            await asyncio.sleep(0.3)
            return True
        await asyncio.sleep(0.05)
    return False


def first_line(s: str) -> str:
    return (s or "").splitlines()[0] if s else ""


# ------------------------------------------------------------- scenario

async def ride(dj, dev: RidePhone, tmp: Path, fx: dict, rep: Report) -> None:
    dead_id = fx["dead_id"]

    print("\n-- 1. bike start: held while Todd talks, then starts ---------------")
    talk(tmp, True)
    announce_in_chat(tmp, "Starting the music in ten seconds, Boss: the ride mix. Pause your book.")
    n = dev.applied
    out = await dj.dj_pool_set(spec=SPEC, allow_empty=True)
    rep.check("held=todd_talking" in out or "HELD (todd_talking)" in out,
              "bike start is HELD while Todd is talking", out[-200:])
    await asyncio.sleep(1.0)
    rep.check(dev.applied == n and not dev.playing, "nothing reached the phone while he talked",
              f"applied {dev.applied - n}")
    talk(tmp, False)
    out = await dj.dj_pool_set(spec=SPEC, allow_empty=True)
    ok = await settle(dev, n)
    rep.check(ok and dev.playing and len(dev.queue) >= 2, "bike start plays once he's done",
              f"{out[:160]} | queue={len(dev.queue)}")
    rep.check(bool(dev.streamed) and all(b > 0 for b in dev.streamed.values()),
              "every track handed to the phone streams real bytes (range GET)",
              f"{len(dev.streamed)} streamed, unplayable={dev.unplayable}")

    print("\n-- 2. a cue fires at a song boundary ------------------------------")
    # A track_end cue arms on the NEXT song: the engine sees it start at the
    # boundary and queues the clip to play right after it.
    nxt = dev.queue[dev.index + 1]["id"]
    out = await dj.dj_spec_note(SPEC, after_track_id=nxt, say="Here comes the climb. Keep pushing.",
                                agent="claude")
    rep.check("rror" not in out[:40], "spec cue saved with a pre-rendered clip", out[:160])
    status = await dj.dj_pool_status()
    before = len(dev.seen)
    await dev.song_ends()
    ok = await seen_after(dev, before, "announce")
    rep.check(ok, "song boundary fired the cue (clip inserted)",
              f"after boundary: {dev.seen[before:]}" + ("" if ok else f" | {status[-400:]}"))

    print("\n-- 3. prev / now / next brief -------------------------------------")
    await dev.song_ends()
    await asyncio.sleep(0.5)
    brief = await dj.dj_break_brief()
    rep.check("Previous:" in brief and "Now playing:" in brief and "Next:" in brief,
              "dj_break_brief names Previous / Now playing / Next",
              " | ".join(l for l in brief.splitlines() if l.split(":")[0] in ("Previous", "Now playing", "Next")))

    print("\n-- 4. dj_mix on the ride spec's sources ---------------------------")
    spec = await dj._get(f"/api/playback/mix-specs/{SPEC}")
    sources = spec.get("sources") or []
    n = dev.applied
    out = await dj.dj_mix(sources=sources, allow_empty=True, seed=7)
    await settle(dev, n)
    rep.check(out.startswith("RESULT") and "held=no" in first_line(out) and "phone_ack=ok" in first_line(out),
              "dj_mix re-plans the ride and the phone acks it", first_line(out))

    print("\n-- 5. shuffle a folder --------------------------------------------")
    folder = next((s["query"] for s in sources if s.get("kind") == "folder"), None)
    n = dev.applied
    out = await dj.dj_folder(folder, action="shuffle", recursive=False) if folder else "no folder source"
    await settle(dev, n)
    rep.check(first_line(out).startswith("RESULT") and "held=no" in first_line(out),
              "dj_folder shuffle goes out through dj_mix", first_line(out) or out[:160])

    print("\n-- 6. dead files --------------------------------------------------")
    live_ids = [q["id"] for q in dev.queue if q["id"] > 0][:2]
    n = dev.applied
    out = await dj.dj_play_now(live_ids[:1] + [dead_id])
    await settle(dev, n)
    line = first_line(out)
    rep.check("Rehearsal Dead Track" in line and "sent=1" in line,
              "play_now drops the dead-path track and names it in RESULT", line)
    rep.check(dead_id not in [q["id"] for q in dev.queue], "the dead track never reached the phone")
    # A file that dies after it was queued: the phone skips it and says so.
    n = dev.applied
    await dj._enqueue_raw("queue", {"track_ids": live_ids[1:2]})
    await settle(dev, n)
    await dev.hit_dead_file(dead_id)
    np = await dj.dj_now_playing()
    rep.check(f"missing_on_phone: track {dead_id}" in np and "skipped=yes" in np,
              "dj_now_playing shows missing_on_phone skipped=yes",
              next((l for l in np.splitlines() if l.startswith("missing_on_phone")), "(none)"))

    print("\n-- 7. quarter-hour chime while talking (engine, in-process) -------")
    from audiplex import dj_pool as pool_mod, dj_triggers
    from audiplex.playback_bus import PlaybackBus
    bus = PlaybackBus()
    pool = pool_mod.DJPool(tmp / "chime_pool.json")
    pool.state["active"] = True
    dj_triggers.set_chime_settings(pool, enabled=True)
    slot = datetime.now().replace(minute=15, second=2, microsecond=0).timestamp()
    bus._states["phone"] = ({"playing": True, "track": {"id": live_ids[0], "title": "x"},
                             "updated_at": slot}, slot)
    talk(tmp, True)
    res = dj_triggers.clock_tick(bus, pool, now=slot)
    rep.check((res["chime"], res["reason"]) == ("dropped", "talking"),
              "a quarter chime while Todd talks is dropped", str(res))
    rep.check(not [c for c in bus._commands.values() if c.type == "bed_play"], "no chime audio queued")

    print("\n-- 8. resume while talking is held -------------------------------")
    mark = len(dev.seen)
    await dj.dj_pause()
    await seen_after(dev, mark, "pause")
    mark = len(dev.seen)
    out = await dj.dj_resume()
    await asyncio.sleep(1.5)
    rep.check(first_line(out).startswith("RESULT sent=0 held=todd_talking") and "resume" not in dev.seen[mark:]
              and not dev.playing, "dj_resume while Todd talks is HELD",
              f"{first_line(out)} | after: {dev.seen[mark:]} playing={dev.playing}")
    talk(tmp, False)
    announce_in_chat(tmp, "Back to the music, Boss.")
    out = await dj.dj_resume()
    await settle(dev, n)
    rep.check(dev.playing, "resume goes through once he's done", first_line(out))

    print("\n-- 9. outro, then end ---------------------------------------------")
    before = len(dev.seen)
    out = await dj.dj_outro("That's the ride. Great work today, Boss.", agent="claude")
    rep.check(out.startswith("Outro armed"), "dj_outro arms after the current song",
              out[:160] + ("" if out.startswith("Outro armed") else f" | phone={dev.state()['track']} playing={dev.playing}"))
    rep.check(await seen_after(dev, before, "announce"), "the outro clip is queued after the current song",
              str(dev.seen[before:]))
    await dev.song_ends()
    await dev.song_ends()  # the outro clip itself ends
    await seen_after(dev, before, "pause", timeout=15)
    rep.check("pause" in dev.seen[before:] or not dev.playing, "the player pauses after the outro",
              str(dev.seen[before:]))
    await dj.dj_pool_stop()


# ------------------------------------------------------------------ main

async def one_ride(run: int, keep: bool) -> Report:
    rep = Report()
    port = free_port()
    if port in FORBIDDEN_PORTS or port == 8100:
        raise SystemExit("REFUSED: rehearsal must never bind :8100")
    tmp = Path(tempfile.mkdtemp(prefix=f"audiplex-ride-{run}-"))
    print(f"\n=== ride rehearsal #{run}: {tmp} (port {port}; live :8100 untouched) ===")
    fx = snapshot(tmp, port)
    tts = FakeTts()
    tts_url = tts.start()
    base = f"http://127.0.0.1:{port}"
    isolate_env(tmp, base, tts_url, fx["token"])
    talk(tmp, False)
    proc = start_server(tmp, port)
    dev = task = None
    try:
        wait_ready(port, proc, tmp, timeout=90)
        dj = importlib.reload(importlib.import_module("audiplex_mcp.server"))
        rep.check(dj.AUDIPLEX_URL == base and ":8100" not in dj.AUDIPLEX_URL,
                  "MCP points at the throwaway server", dj.AUDIPLEX_URL)
        dev = RidePhone(base, fx["token"])
        task = asyncio.create_task(dev.run())
        await asyncio.sleep(0.5)
        await ride(dj, dev, tmp, fx, rep)
    except Exception as e:
        import traceback
        traceback.print_exc()
        rep.check(False, "rehearsal ran to completion", f"{type(e).__name__}: {e}")
    finally:
        if dev:
            dev.stop()
        if task:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        tts.stop()
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    if rep.failed and dev:
        print("phone acks (id, type, status, handle_s, age_s):", dev.acks[-25:])
        print("ack errors:", dev.ack_errors[-5:])
    print(rep.summary())
    if keep or rep.failed:
        print(f"(kept {tmp}; server log: {tmp / 'server.log'})")
    else:
        shutil.rmtree(tmp, ignore_errors=True)
    return rep


async def amain(repeat: int, keep: bool) -> int:
    failed = 0
    for run in range(1, repeat + 1):
        rep = await one_ride(run, keep)
        failed += bool(rep.failed)
    print(f"\nRIDE REHEARSAL: {repeat - failed}/{repeat} ride(s) passed")
    return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--keep", action="store_true", help="keep the temp dir even on success")
    a = ap.parse_args()
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    return asyncio.run(amain(a.repeat, a.keep))


if __name__ == "__main__":
    raise SystemExit(main())
