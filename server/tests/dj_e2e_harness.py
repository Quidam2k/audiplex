"""Audiplex DJ — automated end-to-end harness for all 17 dj_* MCP tools.

Runs the REAL stack: a real uvicorn server on a throwaway port, a real JWT
minted through the real auth code, the real in-memory playback bus, and the
REAL MCP tool functions imported from `audiplex_mcp.server`. The only
simulated piece is the Android device: a stand-in client that long-polls
commands and applies them with the same semantics as PlaybackManager /
Media3, then reports now-playing state back.

Why this exists (item #429): every prior verification of these tools was an
ad-hoc, hand-driven smoke test. This makes it repeatable and automatic, so
Todd's on-device pass is a short confirmation of *Media3 actually moving
audio* rather than the only evidence any of it works.

What this DOES prove: agent -> MCP tool -> HTTP -> auth -> bus -> long-poll
-> client dispatch -> state report -> dj_now_playing read-back, for all 17
tools, including MCP-side name resolution (artist/album/genre/playlist/
favorites) against a real catalog, and the owner-scoped listening signal +
recency cooldown reads (#947/#948) against seeded play history.

What this CANNOT prove: that Media3 physically plays/pauses/seeks audio on
the phone. The simulated client models the queue math, not the audio engine.
That remains Todd's on-device gate.

Run it:

    cd server && C:\Python311\python.exe tests/dj_e2e_harness.py

Use that interpreter explicitly. Audiplex's deps (passlib, PyJWT, fastapi)
live in C:\Python311 — the same interpreter production's launch.bat uses — but
a bare `python` inside a Pantheon worker shell resolves to
Q:\Pantheon\.venv-omnivoice, which lacks passlib and fails at import. The
uvicorn subprocess inherits sys.executable, so whichever Python starts the
harness is the one the server runs on.

Exits 0 if every check passes, 1 otherwise. Touches NOTHING outside a temp
directory — never port 8100, never the real config.yaml, never audiplex.db.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import os
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import yaml

# httpx logs every request at INFO, which buries the actual report.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))
REPO_ROOT = SERVER_DIR.parent

# The live production instance. The harness must never touch it.
FORBIDDEN_PORTS = {8100}


# ----------------------------------------------------------------- reporting

class Report:
    def __init__(self) -> None:
        self.checks: list[tuple[bool, str, str]] = []

    def check(self, ok: bool, label: str, detail: str = "") -> bool:
        self.checks.append((bool(ok), label, detail))
        print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  -- {detail}" if detail else ""))
        return bool(ok)

    @property
    def failed(self) -> list[tuple[bool, str, str]]:
        return [c for c in self.checks if not c[0]]

    def summary(self) -> str:
        n = len(self.checks)
        f = len(self.failed)
        lines = ["", "=" * 66, f"RESULT: {n - f}/{n} checks passed"]
        if f:
            lines.append(f"{f} FAILED:")
            lines += [f"  - {lbl}  {det}" for _, lbl, det in self.failed]
        lines.append("=" * 66)
        return "\n".join(lines)


# ------------------------------------------------------------------- fixture

def free_port() -> int:
    for _ in range(50):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        if port not in FORBIDDEN_PORTS:
            return port
    raise RuntimeError("could not find a free port")


def build_fixture(tmp: Path) -> dict:
    """Write an isolated config.yaml + seed a throwaway SQLite catalog.

    An explicit jwt_secret is written so get_settings() never regenerates one
    (which would try to PERSIST it into a config.yaml — the live server's file
    is a candidate path, so this is a real hazard, not a hypothetical).
    """
    import secrets

    db_path = (tmp / "harness.db").as_posix()
    secret = secrets.token_hex(32)
    config = {
        "database_url": f"sqlite:///{db_path}",
        "jwt_secret": secret,
        "scan_on_startup": False,       # never scan a real library
        "library_roots": [],
        "cover_cache_dir": (tmp / "covers").as_posix(),
        "dj_clip_dir": (tmp / "dj_clips").as_posix(),
        "dj_owner_username": "admin",   # playlist/favorites resolution target
        "host": "127.0.0.1",
    }
    (tmp / "config.yaml").write_text(
        yaml.safe_dump(config, default_flow_style=False), encoding="utf-8"
    )

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from audiplex.auth import create_token, hash_password
    from audiplex.database import Base
    from audiplex.models import (
        Album, Artist, Favorite, Playlist, PlaylistTrack, PlayStat, Track, User,
    )

    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()

    owner = User(username="admin", password_hash=hash_password("x"),
                 display_name="Owner", is_admin=True)
    agent = User(username="dj-agent", password_hash=hash_password("y"),
                 display_name="DJ Agent", is_admin=False)
    session.add_all([owner, agent])
    session.flush()

    def make_album(artist_name: str, album_title: str, genre: str, titles: list[str]):
        artist = Artist(name=artist_name)
        session.add(artist)
        session.flush()
        album = Album(title=album_title, artist_id=artist.id, genre=genre,
                      year=1959, duration_seconds=600.0, track_count=len(titles),
                      folder_path=f"/fake/{artist_name}/{album_title}")
        session.add(album)
        session.flush()
        out = []
        for i, t in enumerate(titles, start=1):
            tr = Track(title=t, album_id=album.id, artist_id=artist.id,
                       disc_number=1, track_number=i, duration_seconds=200.0 + i,
                       file_path=f"/fake/{artist_name}/{album_title}/{i:02d} {t}.mp3",
                       file_size=1_000_000)
            session.add(tr)
            session.flush()
            out.append(tr)
        return out

    jazz = make_album("Miles Davis", "Kind of Blue", "Jazz",
                      ["So What", "Blue in Green", "Flamenco Sketches"])
    classical = make_album("Ludwig van Beethoven", "Symphony No. 5", "Classical",
                           ["Movement I", "Movement II"])

    # A live cut of a track already in the catalog (#948): a DIFFERENT
    # recording of the SAME work. Cooldown must catch it; ratings must not
    # merge it. Its duration is deliberately far from the studio take so only
    # the work key can connect them.
    live_album = Album(title="Live In Europe", artist_id=jazz[0].artist_id,
                       genre="Jazz", year=1967, duration_seconds=480.0,
                       track_count=1, folder_path="/fake/Miles Davis/Live In Europe")
    session.add(live_album)
    session.flush()
    live_take = Track(title="So What (Live)", album_id=live_album.id,
                      artist_id=jazz[0].artist_id, disc_number=1, track_number=1,
                      duration_seconds=480.0,
                      file_path="/fake/Miles Davis/Live In Europe/01 So What.mp3",
                      file_size=2_000_000)
    session.add(live_take)
    session.flush()

    # Owner's playlist + favorites — these are the point of the owner-resolved
    # endpoints: dj-agent has none of its own.
    pl = Playlist(name="Road Trip", user_id=owner.id)
    session.add(pl)
    session.flush()
    session.add_all([
        PlaylistTrack(playlist_id=pl.id, track_id=jazz[0].id, position=0),
        PlaylistTrack(playlist_id=pl.id, track_id=classical[0].id, position=1),
    ])
    session.add_all([
        Favorite(entity_type="track", entity_key=str(jazz[1].id), user_id=owner.id),
        Favorite(entity_type="track", entity_key=str(classical[1].id), user_id=owner.id),
    ])

    # Listening history for the OWNER (#947/#948). dj-agent has none of its
    # own, which is the whole point of the owner-scoped reads.
    #   'So What'    — just heard, so it is inside the cooldown window.
    #   'Movement I' — heard two hours ago: out of cooldown, but its history
    #                  still shows a poor completion rate and early bails.
    now = datetime.now(timezone.utc)
    long_ago = now - timedelta(hours=2)
    session.add_all(
        [PlayStat(track_id=jazz[0].id, user_id=owner.id, event="start",
                  played_seconds=0.0, timestamp=now)]
        + [PlayStat(track_id=jazz[0].id, user_id=owner.id, event="complete",
                    played_seconds=201.0, timestamp=now)]
        + [PlayStat(track_id=classical[0].id, user_id=owner.id, event="start",
                    played_seconds=0.0, timestamp=long_ago) for _ in range(4)]
        + [PlayStat(track_id=classical[0].id, user_id=owner.id, event="complete",
                    played_seconds=201.0, timestamp=long_ago)]
        + [PlayStat(track_id=classical[0].id, user_id=owner.id, event="skip",
                    played_seconds=5.0, timestamp=long_ago) for _ in range(2)]
    )
    session.commit()

    token = create_token(agent.id, agent.username, secret, 24)
    ids = {
        "jazz": [t.id for t in jazz],
        "classical": [t.id for t in classical],
        "playlist": [jazz[0].id, classical[0].id],
        "favorites": [jazz[1].id, classical[1].id],
        "live_variant": live_take.id,    # same work as jazz[0], different recording
        "recently_played": jazz[0].id,   # inside the cooldown window
        "long_ago": classical[0].id,     # history, but out of cooldown
    }
    session.close()
    engine.dispose()
    return {"token": token, "ids": ids, "db_path": db_path}


def start_server(tmp: Path, port: int) -> subprocess.Popen:
    """Launch a real uvicorn with cwd=tmp so it picks up the isolated config."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SERVER_DIR)
    env["PYTHONUNBUFFERED"] = "1"
    log = open(tmp / "server.log", "wb")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "audiplex.main:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=str(tmp), env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    return proc


def wait_ready(port: int, proc: subprocess.Popen, tmp: Path, timeout: float = 45.0) -> None:
    deadline = time.time() + timeout
    url = f"http://127.0.0.1:{port}/docs"
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"server died during startup (exit {proc.returncode}):\n"
                + (tmp / "server.log").read_text(errors="replace")[-2500:]
            )
        try:
            if httpx.get(url, timeout=2).status_code == 200:
                return
        except Exception:
            time.sleep(0.4)
    raise RuntimeError(
        "server never became ready:\n"
        + (tmp / "server.log").read_text(errors="replace")[-2500:]
    )


# ------------------------------------------------------------- fake TTS

class FakeTts:
    """A minimal OpenAI-compatible /v1/audio/speech endpoint.

    Exercises the REAL primary TTS path (locked decision: OpenAI-compatible
    URL, no Pantheon dependency) without needing a model on the box, and
    captures the request so the payload shape is asserted rather than assumed.
    """

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.wav = self._silent_wav()
        self.port = free_port()
        self._srv = None

    @staticmethod
    def _silent_wav(seconds: float = 0.4, rate: int = 8000) -> bytes:
        import io
        import wave

        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(b"\x00\x00" * int(rate * seconds))
        return buf.getvalue()

    def start(self) -> str:
        import json as _json
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                try:
                    outer.requests.append(_json.loads(body))
                except Exception:
                    outer.requests.append({"_unparsed": body[:200].decode(errors="replace")})
                self.send_response(200)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Content-Length", str(len(outer.wav)))
                self.end_headers()
                self.wfile.write(outer.wav)

            def log_message(self, *a):  # keep the harness output clean
                pass

        self._srv = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        if self._srv:
            self._srv.shutdown()
            self._srv.server_close()


# ------------------------------------------------------- simulated device

class SimulatedDevice:
    """Stand-in for the Android client (DjCommandClient + PlaybackManager).

    Mirrors the real Media3 queue semantics: play_now replaces, queue appends,
    play_next inserts after the current item, reorder moves while keeping the
    playing item pinned, skip/previous walk the index.
    """

    def __init__(self, base: str, token: str) -> None:
        self.base = base
        self.headers = {"Authorization": f"Bearer {token}"}
        self.queue: list[dict] = []
        self.index = 0
        self.playing = False
        self.position_ms = 0
        self.volume: float | None = None
        self.applied = 0            # commands executed
        self.seen: list[str] = []   # command types, in order
        self.catalog: dict[int, dict] = {}
        self._stop = False

    async def load_catalog(self, client: httpx.AsyncClient) -> None:
        """Resolve track ids -> metadata, like the real client's resolveTracks."""
        artists = (await client.get(f"{self.base}/api/music/artists",
                                    headers=self.headers)).json()
        for a in artists:
            tracks = (await client.get(
                f"{self.base}/api/music/artists/{a['id']}/tracks",
                headers=self.headers)).json()
            for t in tracks:
                self.catalog[t["id"]] = {
                    "id": t["id"],
                    "title": t.get("title"),
                    "artist": t.get("artist_name") or a.get("name"),
                    "duration_ms": int((t.get("duration_seconds") or 0) * 1000),
                }

    def _items(self, track_ids: list[int]) -> list[dict]:
        return [self.catalog.get(i, {"id": i, "title": f"track {i}",
                                     "artist": None, "duration_ms": 0})
                for i in track_ids]

    def apply(self, ctype: str, payload: dict) -> None:
        cur = self.queue[self.index] if 0 <= self.index < len(self.queue) else None
        if ctype == "play_now":
            self.queue = self._items(payload.get("track_ids", []))
            self.index, self.playing, self.position_ms = 0, True, 0
        elif ctype == "play_stream":
            self.queue = [{"id": -1, "title": payload.get("title", "Live stream"),
                           "artist": "Stream", "duration_ms": 0,
                           "url": payload.get("url")}]
            self.index, self.playing, self.position_ms = 0, True, 0
        elif ctype == "queue":
            was_empty = not self.queue
            self.queue.extend(self._items(payload.get("track_ids", [])))
            if was_empty:
                self.index, self.playing, self.position_ms = 0, True, 0
        elif ctype == "play_next":
            new = self._items(payload.get("track_ids", []))
            if not self.queue:
                self.queue = new
                self.index, self.playing, self.position_ms = 0, True, 0
            else:
                at = self.index + 1
                self.queue[at:at] = new
        elif ctype == "announce":
            # Mirrors PlaybackManager.insertVoiceClip: a synthetic negative-id
            # item built from the clip URL directly, inserted after the current
            # track (mode 'next') or played immediately (mode 'now').
            clip = {
                "id": -int(payload.get("clip_id", 0)),
                "title": payload.get("title", "DJ break"),
                "artist": "DJ",
                "duration_ms": int((payload.get("duration_seconds") or 0) * 1000),
                "clip_url": payload.get("clip_url"),
            }
            if not self.queue or payload.get("mode") == "now":
                self.queue.insert(self.index if self.queue else 0, clip)
                self.playing, self.position_ms = True, 0
            else:
                self.queue.insert(self.index + 1, clip)
        elif ctype == "reorder":
            f, t = payload.get("from_index", 0), payload.get("to_index", 0)
            if 0 <= f < len(self.queue) and 0 <= t < len(self.queue):
                item = self.queue.pop(f)
                self.queue.insert(t, item)
                if cur is not None:               # keep the playing item pinned
                    self.index = self.queue.index(cur)
        elif ctype == "skip":
            if self.index + 1 < len(self.queue):
                self.index += 1
                self.position_ms = 0
        elif ctype == "previous":
            if self.index > 0:
                self.index -= 1
                self.position_ms = 0
        elif ctype == "pause":
            self.playing = False
        elif ctype == "resume":
            self.playing = True
        elif ctype == "seek":
            self.position_ms = int(payload.get("position_ms", 0))
        elif ctype == "volume":
            self.volume = float(payload.get("volume", 0.0))
        else:
            self.seen.append(f"UNKNOWN:{ctype}")
            return
        self.seen.append(ctype)

    def state(self) -> dict:
        cur = self.queue[self.index] if 0 <= self.index < len(self.queue) else None
        return {
            "playing": self.playing,
            "track": ({"id": cur["id"], "title": cur["title"], "artist": cur["artist"]}
                      if cur else None),
            "position_ms": self.position_ms,
            "duration_ms": (cur or {}).get("duration_ms", 0),
            "queue_length": len(self.queue),
            "queue_index": self.index,
            "queue": [{"index": i, "id": it["id"], "title": it["title"],
                       "artist": it["artist"]} for i, it in enumerate(self.queue)],
            "volume": self.volume,
        }

    async def run(self) -> None:
        """Long-poll -> dispatch -> report, exactly like DjCommandClient."""
        async with httpx.AsyncClient(timeout=40) as client:
            await self.load_catalog(client)
            await self._report(client)
            while not self._stop:
                try:
                    r = await client.get(f"{self.base}/api/playback/command/next",
                                         headers=self.headers)
                    if r.status_code == 204:
                        continue                      # long-poll timeout; re-issue
                    r.raise_for_status()
                    cmd = r.json()
                    self.apply(cmd["type"], cmd.get("payload") or {})
                    await self._report(client)
                    self.applied += 1
                except httpx.ReadTimeout:
                    continue
                except Exception:
                    if self._stop:
                        return
                    await asyncio.sleep(0.2)

    async def _report(self, client: httpx.AsyncClient) -> None:
        await client.post(f"{self.base}/api/playback/state",
                          headers=self.headers, json=self.state())

    def stop(self) -> None:
        self._stop = True


# ----------------------------------------------------------------- scenario

async def wait_applied(dev: SimulatedDevice, target: int, timeout: float = 12.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if dev.applied >= target:
            await asyncio.sleep(0.15)   # let the state POST land
            return True
        await asyncio.sleep(0.05)
    return False


async def run_scenario(dev: SimulatedDevice, dj, ids: dict, rep: Report, tts: "FakeTts") -> None:
    jazz, classical = ids["jazz"], ids["classical"]
    n = 0

    async def step(coro, label: str) -> str:
        nonlocal n
        out = await coro
        n += 1
        ok = await wait_applied(dev, n)
        rep.check(ok, f"{label} -> reached the device", "" if ok else "command never applied")
        return out

    print("\n-- transport & queue ------------------------------------------")

    await step(dj.dj_play_now(jazz), "dj_play_now")
    rep.check([i["id"] for i in dev.queue] == jazz and dev.index == 0 and dev.playing,
              "dj_play_now set the queue and started playback", f"queue={[i['id'] for i in dev.queue]}")

    np = await dj.dj_now_playing()
    rep.check("So What" in np and "Miles Davis" in np,
              "dj_now_playing reports the right track", np.splitlines()[0] if np else "")
    rep.check(np.count("\n") >= 3 and "Queue:" in np,
              "dj_now_playing lists the queue with indices")

    await step(dj.dj_queue([classical[0]]), "dj_queue")
    rep.check([i["id"] for i in dev.queue] == jazz + [classical[0]],
              "dj_queue appended to the END", f"queue={[i['id'] for i in dev.queue]}")

    await step(dj.dj_play_next([classical[1]]), "dj_play_next")
    expect = [jazz[0], classical[1], jazz[1], jazz[2], classical[0]]
    rep.check([i["id"] for i in dev.queue] == expect,
              "dj_play_next inserted AFTER the current track",
              f"got={[i['id'] for i in dev.queue]} want={expect}")

    playing_before = dev.queue[dev.index]["id"]
    await step(dj.dj_reorder(1, 3), "dj_reorder")
    rep.check([i["id"] for i in dev.queue] == [jazz[0], jazz[1], jazz[2], classical[1], classical[0]],
              "dj_reorder moved the track", f"queue={[i['id'] for i in dev.queue]}")
    rep.check(dev.queue[dev.index]["id"] == playing_before,
              "dj_reorder kept the playing track pinned")

    await step(dj.dj_skip(), "dj_skip")
    rep.check(dev.index == 1, "dj_skip advanced the index", f"index={dev.index}")

    await step(dj.dj_previous(), "dj_previous")
    rep.check(dev.index == 0, "dj_previous went back", f"index={dev.index}")

    await step(dj.dj_pause(), "dj_pause")
    rep.check(dev.playing is False, "dj_pause stopped playback")

    await step(dj.dj_resume(), "dj_resume")
    rep.check(dev.playing is True, "dj_resume restarted playback")

    await step(dj.dj_seek(120), "dj_seek")
    rep.check(dev.position_ms == 120_000,
              "dj_seek converted seconds -> ms", f"position_ms={dev.position_ms}")

    await step(dj.dj_volume(30), "dj_volume")
    rep.check(abs((dev.volume or 0) - 0.30) < 1e-6,
              "dj_volume converted 0-100 -> 0.0-1.0", f"volume={dev.volume}")
    np = await dj.dj_now_playing()
    rep.check("volume: 30%" in np, "dj_now_playing reports volume back as a percentage")
    bad = await dj.dj_volume(150)
    rep.check("must be 0-100" in bad, "dj_volume rejects out-of-range levels", bad)

    print("\n-- MCP-side name resolution ----------------------------------")

    # Every Miles Davis track, both albums — Kind of Blue first, then the live
    # take, ordered by album title as /artists/{id}/tracks returns them.
    all_miles = jazz + [ids["live_variant"]]
    await step(dj.dj_queue_by("Miles", "artist", "now"), "dj_queue_by(artist)")
    rep.check([i["id"] for i in dev.queue] == all_miles,
              "dj_queue_by resolved an artist by PREFIX", f"queue={[i['id'] for i in dev.queue]}")

    await step(dj.dj_queue_by("Kind of Blue", "album", "now"), "dj_queue_by(album)")
    rep.check([i["id"] for i in dev.queue] == jazz, "dj_queue_by resolved an album")

    await step(dj.dj_queue_by("Classical", "genre", "now"), "dj_queue_by(genre)")
    rep.check([i["id"] for i in dev.queue] == classical,
              "dj_queue_by resolved a genre", f"queue={[i['id'] for i in dev.queue]}")

    await step(dj.dj_queue_by("Road Trip", "playlist", "now"), "dj_queue_by(playlist)")
    rep.check([i["id"] for i in dev.queue] == ids["playlist"],
              "dj_queue_by resolved the OWNER's playlist (not the agent's empty one)",
              f"queue={[i['id'] for i in dev.queue]}")

    await step(dj.dj_queue_by("", "favorites", "now"), "dj_queue_by(favorites)")
    rep.check(sorted(i["id"] for i in dev.queue) == sorted(ids["favorites"]),
              "dj_queue_by resolved the OWNER's favorites",
              f"queue={[i['id'] for i in dev.queue]}")

    miss = await dj.dj_queue_by("zzz-nope", "artist", "queue")
    rep.check("No artist matching" in miss, "dj_queue_by fails gracefully on no match", miss)
    badmode = await dj.dj_queue_by("Miles", "artist", "sideways")
    rep.check("Unknown mode" in badmode, "dj_queue_by rejects a bad mode", badmode)

    print("\n-- cross-app source switch (#439 Phase 2) --------------------")

    await step(dj.dj_play_stream("http://127.0.0.1:9/stream.mp3", "Radio Free Luna"),
               "dj_play_stream")
    rep.check(len(dev.queue) == 1 and dev.queue[0].get("url", "").endswith("/stream.mp3"),
              "dj_play_stream replaced the queue with the stream", f"queue={dev.queue}")
    np = await dj.dj_now_playing()
    rep.check("Radio Free Luna" in np, "dj_now_playing reports the stream title")

    await step(dj.dj_play_now(jazz), "dj_play_now (switch back from stream)")
    rep.check([i["id"] for i in dev.queue] == jazz,
              "switched back from the stream to music")

    print("\n-- DJ voice breaks (#431) ------------------------------------")

    brief = await dj.dj_break_brief()
    rep.check("Daypart:" in brief and "Directive:" in brief,
              "dj_break_brief returns a dayparted persona directive",
              brief.splitlines()[1] if brief.count("\n") else brief[:80])
    rep.check("Now playing:" in brief,
              "dj_break_brief includes the now-playing snapshot")
    rep.check("Style:" in brief and "no markdown" in brief,
              "dj_break_brief carries the spoken-copy style rules")
    rep.check("WARNING: no TTS backend" not in brief,
              "dj_break_brief sees the configured TTS backend")

    copy = "You're listening to the late shift. That was Miles Davis. Stay with me."
    before = [i["id"] for i in dev.queue]
    cur_id = dev.queue[dev.index]["id"]
    out = await step(dj.dj_announce(copy, "next", "Test break"), "dj_announce")
    rep.check("voice break" in out, "dj_announce reports a queued break", out)

    rep.check(len(dev.queue) == len(before) + 1,
              "announce inserted exactly one item", f"queue={[i['id'] for i in dev.queue]}")
    rep.check(dev.queue[dev.index]["id"] == cur_id,
              "announce did NOT interrupt the current track (mode='next')")
    clip_item = dev.queue[dev.index + 1]
    rep.check(clip_item["id"] < 0,
              "the break is a synthetic negative-id item (won't hit the catalog)",
              f"id={clip_item['id']}")
    rep.check(clip_item["title"] == "Test break", "the break carries its title")

    # The TTS request shape is the contract with any OpenAI-compatible server.
    rep.check(len(tts.requests) == 1, f"TTS was called once ({len(tts.requests)})")
    if tts.requests:
        req = tts.requests[-1]
        rep.check(req.get("input") == copy, "TTS received the agent's exact copy")
        rep.check({"model", "input", "voice", "response_format"} <= set(req),
                  "TTS payload matches the OpenAI speech contract",
                  f"keys={sorted(req)}")

    # The clip must actually be fetchable by the device, with range support.
    clip_url = clip_item["clip_url"]
    rep.check(bool(clip_url) and clip_url.startswith("/api/dj/clips/"),
              "the break carries a relative clip URL the client can resolve",
              str(clip_url))
    async with httpx.AsyncClient(timeout=15) as c:
        got = await c.get(f"{dev.base}{clip_url}", headers=dev.headers)
        rep.check(got.status_code == 200 and got.content == tts.wav,
                  "the device can fetch the exact synthesized audio back",
                  f"status={got.status_code} bytes={len(got.content)}")
        ranged = await c.get(f"{dev.base}{clip_url}",
                             headers={**dev.headers, "Range": "bytes=0-49"})
        rep.check(ranged.status_code == 206 and len(ranged.content) == 50,
                  "clip serving supports range requests (ExoPlayer needs this)",
                  f"status={ranged.status_code}")

    npa = await dj.dj_now_playing()
    rep.check("Test break" in npa, "dj_now_playing shows the break in the queue")

    bad = await dj.dj_announce("hi", "sideways")
    rep.check("Unknown mode" in bad, "dj_announce rejects a bad mode", bad)
    empty = await dj.dj_announce("   ")
    rep.check("nothing to announce" in empty, "dj_announce guards empty copy", empty)

    print("\n-- listening signal + recency cooldown (#947/#948) -----------")

    stats = await dj.dj_track_stats(min_starts=1)
    rep.check("Movement I" in stats, "dj_track_stats reports the owner's history",
              stats.splitlines()[1] if len(stats.splitlines()) > 1 else stats)
    rep.check("25% finished" in stats,
              "completion RATE, not a raw count (1 of 4 starts finished)")
    rep.check("first 10s" in stats,
              "early bails are called out with where they land")

    picks = await dj.dj_check_picks([ids["recently_played"], ids["long_ago"]])
    rep.check(f"Clear to play (1): [{ids['long_ago']}]" in picks,
              "dj_check_picks clears a track heard two hours ago", picks.splitlines()[1])
    rep.check("this exact recording played" in picks,
              "dj_check_picks flags the track just heard, with the reason")

    live_pick = await dj.dj_check_picks([ids["live_variant"]])
    rep.check("another version of this song" in live_pick,
              "a live cut of a just-played song is caught at WORK level",
              live_pick.splitlines()[-2] if len(live_pick.splitlines()) > 1 else live_pick)

    async with httpx.AsyncClient(timeout=10) as c:
        ident = (await c.get(
            f"{dev.base}/api/playback/tracks/{ids['recently_played']}/identity",
            headers=dev.headers,
        )).json()
    rep.check(ids["live_variant"] in ident.get("same_work_track_ids", []),
              "studio and live share a work id", str(ident.get("same_work_track_ids")))
    rep.check(ids["live_variant"] not in ident.get("same_recording_track_ids", []),
              "...but NOT a recording id, so their ratings stay separate",
              str(ident.get("same_recording_track_ids")))

    n_before = dev.applied
    advised = await dj.dj_play_next([ids["recently_played"]])
    rep.check("Heads-up" in advised and "played recently" in advised,
              "queueing a just-played track warns instead of silently filtering",
              advised.splitlines()[-1].strip())
    rep.check(await wait_applied(dev, n_before + 1),
              "...and the command STILL reached the device — advisory, not a veto")
    n = dev.applied

    print("\n-- guards ----------------------------------------------------")

    no_picks = await dj.dj_check_picks([])
    rep.check("nothing to check" in no_picks, "dj_check_picks guards an empty list",
              no_picks)

    empty = await dj.dj_play_now([])
    rep.check("nothing to play" in empty, "dj_play_now guards an empty track list", empty)
    rep.check(dev.applied == n, "no phantom commands reached the device",
              f"applied={dev.applied} sent={n}")

    expected_types = {
        "play_now", "queue", "play_next", "reorder", "skip", "previous",
        "pause", "resume", "seek", "volume", "play_stream", "announce",
    }
    rep.check(expected_types.issubset(set(dev.seen)),
              "every command type was dispatched by the client",
              f"missing={sorted(expected_types - set(dev.seen))}")
    rep.check(not [s for s in dev.seen if s.startswith("UNKNOWN")],
              "client understood every command type it received",
              f"unknown={[s for s in dev.seen if s.startswith('UNKNOWN')]}")


# --------------------------------------------------------------------- main

async def amain() -> int:
    rep = Report()
    port = free_port()
    assert port not in FORBIDDEN_PORTS, "refusing to bind the production port"
    tmp = Path(tempfile.mkdtemp(prefix="audiplex-dj-e2e-"))
    print(f"Audiplex DJ end-to-end harness")
    print(f"  temp dir : {tmp}")
    print(f"  port     : {port}  (production :8100 untouched)")

    fixture = build_fixture(tmp)
    tts = FakeTts()
    tts_url = tts.start()
    print(f"  fake TTS : {tts_url}  (OpenAI-compatible /v1/audio/speech)")
    proc = start_server(tmp, port)
    dev_task = None
    dev = None
    try:
        wait_ready(port, proc, tmp)
        base = f"http://127.0.0.1:{port}"
        print(f"  server   : up at {base}")

        # The MCP module reads AUDIPLEX_URL/AUDIPLEX_TOKEN at IMPORT time, so
        # the env must be set before the import (and the module reloaded if a
        # previous run already imported it).
        os.environ["AUDIPLEX_URL"] = base
        os.environ["AUDIPLEX_TOKEN"] = fixture["token"]
        # tts_backend reads its env per call, so these can be set after import.
        os.environ["DJ_TTS_URL"] = tts_url
        os.environ["DJ_TTS_FORMAT"] = "wav"
        os.environ["DJ_PERSONA_NAME"] = "the DJ"   # persona-agnostic until Todd names it
        os.environ.pop("DJ_TTS_CMD", None)
        os.environ.pop("DJ_LAT", None)             # keep the harness offline
        os.environ.pop("DJ_LON", None)
        if str(REPO_ROOT) not in sys.path:
            sys.path.insert(0, str(REPO_ROOT))
        dj = importlib.import_module("audiplex_mcp.server")
        dj = importlib.reload(dj)
        rep.check(dj.AUDIPLEX_URL == base, "MCP server points at the throwaway instance",
                  dj.AUDIPLEX_URL)

        tools = await dj.mcp.list_tools()
        names = {t.name for t in tools}
        expected_tools = {
            "dj_play_now", "dj_skip", "dj_queue", "dj_play_next", "dj_reorder",
            "dj_pause", "dj_resume", "dj_previous", "dj_seek", "dj_volume",
            "dj_play_stream", "dj_queue_by", "dj_now_playing",
            "dj_break_brief", "dj_announce",
            "dj_check_picks", "dj_track_stats",
        }
        rep.check(expected_tools.issubset(names),
                  f"all 17 DJ tools register ({len(names)} found)",
                  f"missing={sorted(expected_tools - names)}")

        idle = await dj.dj_now_playing()
        rep.check("Nothing is playing" in idle, "clean bus reports nothing playing", idle)

        dev = SimulatedDevice(base, fixture["token"])
        dev_task = asyncio.create_task(dev.run())
        await asyncio.sleep(1.0)          # let the catalog load + first report
        rep.check(len(dev.catalog) == 6, "simulated device loaded the catalog",
                  f"{len(dev.catalog)} tracks")

        await run_scenario(dev, dj, fixture["ids"], rep, tts)
    except Exception as e:
        rep.check(False, "harness ran to completion", f"{type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
    finally:
        if dev:
            dev.stop()
        if dev_task:
            dev_task.cancel()
            try:
                await dev_task
            except (asyncio.CancelledError, Exception):
                pass
        tts.stop()
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    print(rep.summary())
    print(f"(server log: {tmp / 'server.log'})")
    return 1 if rep.failed else 0


def main() -> int:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    return asyncio.run(amain())


if __name__ == "__main__":
    raise SystemExit(main())
