"""#2021 isolated end-to-end: the PC renderer plays the shared queue and hands off.

    C:\\Python311\\python.exe windows_client/scripts/e2e_handoff_isolated.py [--port 8199]
    ... --no-transfer   # the red run: skips the transfers, so the handoff asserts fail

Stands up a throwaway server (temp config.yaml + fresh temp DB) on 127.0.0.1:<port>
with three DIGITALLY SILENT tracks. Runs the REAL PC Player/BusClient/AuthProxy as
device "e2e-pc" on VLC's dummy output at volume 0 (asserted before anything plays),
next to a scripted fake renderer "e2e-test-device". Then checks:
  (a) a play_now sent while the PC is active plays on the PC and the server sees it;
  (b) the test device's polls meanwhile get nothing (no double play);
  (c) transfer to the test device: the PC pauses and the test device is told to
      resume the same track at the PC's position (+-2 s);
  (d) transfer back: the test device pauses and the PC resumes at its position.
Before (a), the phone-first leg (p) (#7149): a fake PHONE (paramless polls, exactly
how the real phone talks to the bus) is mid-track after a seek with nothing declared
active; transferring to the PC must pause it and start the PC on the same track,
the same position (+-2 s) and the same queue. After (d), the book leg (k): the fake
phone is in an audiobook (track None + a fresh progress save, as the phone reports
it); transferring to the PC must open that book at the phone's position.
Refuses to run on :8100, on the live DB, on a busy port, or with a device id that
could collide with a real renderer. The fake phone uses the legacy "phone" id, but
only on the throwaway server, which the real phone never talks to. Never touches :8100, the phone or a running PC
renderer. Exit code 0 = every check passed.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx

logging.getLogger("httpx").setLevel(logging.WARNING)

REPO = Path(__file__).resolve().parents[2]
SERVER = REPO / "server"
LIVE_PORT = 8100
LIVE_DB = (SERVER / "audiplex.db").resolve()
PC_ID = "e2e-pc"
TEST_ID = "e2e-test-device"
PHONE_USER = "e2e-phone"
PHONE_SEEK_MS = 30_000
BOOK_SEEK_MS = 125_000
TOLERANCE_MS = 2000
sys.path.insert(0, str(REPO / "windows_client"))
sys.path.insert(0, str(SERVER))

from audiplex.playback_bus import LEGACY_DEVICE_ID  # noqa: E402

failures: list[str] = []
t0 = time.monotonic()


def note(msg: str) -> None:
    print(f"[{time.monotonic() - t0:6.2f}s] {msg}", flush=True)


def check(ok: bool, what: str) -> bool:
    note(("PASS " if ok else "FAIL ") + what)
    if not ok:
        failures.append(what)
    return ok


def _guard(port: int, db_path: Path) -> None:
    if port == LIVE_PORT:
        sys.exit(f"refusing to run against the live port {LIVE_PORT}")
    if db_path.resolve() == LIVE_DB:
        sys.exit(f"refusing to run against the live DB {LIVE_DB}")
    for device_id in (PC_ID, TEST_ID):
        if device_id == LEGACY_DEVICE_ID or not device_id.startswith("e2e-"):
            sys.exit(f"refusing: device id {device_id!r} could collide with a real renderer")
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", port)) == 0:
            sys.exit(f"refusing: something is already listening on :{port}")


def _mint(tmp: Path, env: dict, username: str | None) -> str:
    cmd = [sys.executable, "-m", "audiplex.create_service_token"]
    if username:
        cmd += ["--username", username]
    out = subprocess.run(cmd, cwd=tmp, env=env, capture_output=True, text=True, check=True).stdout
    for line in out.splitlines():
        if "AUDIPLEX_TOKEN=" in line:
            return line.split("AUDIPLEX_TOKEN=", 1)[1].strip()
    raise RuntimeError(f"no token in: {out}")


def _wait(predicate, timeout: float, what: str):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.25)
    raise TimeoutError(what)


def _ok(url: str) -> bool:
    try:
        return httpx.get(url, timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


class FakeRenderer:
    """A scripted second device: polls the bus like the phone, keeps a clock."""

    def __init__(self, base: str, token: str, device_id: str | None = TEST_ID) -> None:
        # device_id None = the real phone's paramless polls and reports.
        self.device_id = device_id
        self.client = httpx.Client(base_url=base, headers={"Authorization": f"Bearer {token}"}, timeout=35)
        self.received: list[dict] = []
        self.track_ids: list[int] = []
        self.playing = False
        self._pos_ms = 0
        self._since = 0.0
        self._stop = threading.Event()
        self._lock = threading.Lock()

    def position_ms(self) -> int:
        with self._lock:
            extra = int((time.monotonic() - self._since) * 1000) if self.playing else 0
            return self._pos_ms + extra

    def state(self) -> dict:
        tid = self.track_ids[0] if self.track_ids else None
        return {
            "playing": self.playing,
            "track": {"id": tid, "title": "fake", "artist": "fake"} if tid else None,
            "position_ms": self.position_ms(),
            "duration_ms": 120_000 if tid else 0,
            "queue_length": len(self.track_ids),
            "queue_index": 0,
            "queue": [{"index": i, "id": t, "title": "fake", "artist": "fake"}
                      for i, t in enumerate(self.track_ids)],
            "volume": 0.0,
            "book": None,
        }

    def report(self) -> None:
        params = {"device_id": self.device_id} if self.device_id else {}
        self.client.post("/api/playback/state", params=params, json=self.state())

    def start(self) -> None:
        threading.Thread(target=self._poll, daemon=True).start()
        threading.Thread(target=self._report, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def play(self, track_ids: list[int], position_ms: int) -> None:
        """Start playing locally (empty track_ids = an audiobook, as the phone reports one)."""
        with self._lock:
            self.track_ids = list(track_ids)
            self._pos_ms = position_ms
            self._since = time.monotonic()
            self.playing = True
        self.report()

    def _report(self) -> None:
        while not self._stop.wait(1):
            try:
                self.report()
            except httpx.HTTPError:
                pass

    def _poll(self) -> None:
        while not self._stop.is_set():
            try:
                params = {"device_id": self.device_id, "device_name": "E2E Test Device",
                          "device_type": "test"} if self.device_id else {}
                r = self.client.get("/api/playback/command/next", params=params)
            except httpx.HTTPError:
                continue
            if r.status_code == 204:
                continue
            cmd = r.json()
            self.received.append(cmd)
            payload = cmd.get("payload") or {}
            with self._lock:
                if cmd.get("type") == "activate":
                    self.track_ids = list(payload.get("track_ids") or [])
                    self._pos_ms = int(payload.get("position_ms") or 0)
                    self._since = time.monotonic()
                    self.playing = bool(payload.get("playing")) and bool(self.track_ids)
                elif cmd.get("type") == "deactivate":
                    self._pos_ms += int((time.monotonic() - self._since) * 1000) if self.playing else 0
                    self.playing = False
            if cmd.get("type") == "deactivate":
                self.report()  # the exact spot goes up before the ack, like the phone
            self.client.post(f"/api/playback/command/{cmd['id']}/ack", json={"status": "ok"})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8199)
    ap.add_argument("--no-transfer", action="store_true", help="red run: skip the transfers")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="audiplex-e2e-2021-"))
    db_path = tmp / "audiplex.db"
    _guard(args.port, db_path)

    base = f"http://127.0.0.1:{args.port}"
    music = tmp / "lib" / "music" / "E2E Artist" / "E2E Album"
    music.mkdir(parents=True)
    for n in (1, 2, 3):
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                        "anullsrc=r=44100:cl=stereo", "-c:a", "aac", "-t", "120",
                        "-metadata", f"title=E2E Silence {n}", "-metadata", "artist=E2E Artist",
                        "-metadata", "album=E2E Album", "-metadata", f"track={n}",
                        str(music / f"0{n} E2E Silence {n}.m4a")], check=True)
    book_dir = tmp / "lib" / "books" / "E2E Author" / "E2E Book"
    book_dir.mkdir(parents=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    "anullsrc=r=44100:cl=stereo", "-c:a", "aac", "-t", "600",
                    "-metadata", "title=E2E Book", "-metadata", "artist=E2E Author",
                    "-metadata", "album=E2E Book", str(book_dir / "E2E Book.m4b")], check=True)
    (tmp / "config.yaml").write_text(json.dumps({
        "library_roots": [{"path": str(tmp / "lib" / "music"), "category": "music"},
                          {"path": str(tmp / "lib" / "books"), "category": "audiobook_clean"}],
        "database_url": f"sqlite:///{db_path.as_posix()}",
        "port": args.port,
        "cover_cache_dir": str(tmp / "covers"),
        "dj_clip_dir": str(tmp / "dj_clips"),
        "jwt_secret": "e2e-2021-" + "0" * 40,
    }), encoding="utf-8")
    env = {
        **os.environ,
        "PYTHONPATH": str(SERVER),
        "AUDIPLEX_EXIT_LOG": str(tmp / "exits.jsonl"),
        "AUDIPLEX_LINK_LOG": str(tmp / "link.jsonl"),
        "AUDIPLEX_DIAG_LOG": str(tmp / "diag.jsonl"),
        "AUDIPLEX_DJ_POOL_STATE": str(tmp / "pool.json"),
        "AUDIPLEX_CHIME_CACHE_DIR": str(tmp / "chimes"),
    }
    log = open(tmp / "server.log", "w", encoding="utf-8")
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "audiplex.main:app", "--host", "127.0.0.1", "--port", str(args.port)],
        cwd=tmp, env=env, stdout=log, stderr=subprocess.STDOUT,
    )

    bus = proxy = player = fake = phone = None
    try:
        _wait(lambda: _ok(f"{base}/docs"), 60, "server up")
        dj = httpx.Client(base_url=base, headers={"Authorization": f"Bearer {_mint(tmp, env, None)}"}, timeout=30)
        pc_token = _mint(tmp, env, PC_ID)
        test_token = _mint(tmp, env, TEST_ID)
        phone_token = _mint(tmp, env, PHONE_USER)
        dj.post("/api/library/scan")

        def tracks() -> list[int]:
            ids: list[int] = []
            for artist in dj.get("/api/music/artists").json():
                ids += [t["id"] for t in dj.get(f"/api/music/artists/{artist['id']}/tracks").json()]
            return ids if len(ids) >= 3 else []

        track_ids = sorted(_wait(tracks, 60, "tracks scanned"))[:3]
        book_id = _wait(lambda: next((b["id"] for b in dj.get("/api/library/books").json()), None),
                        60, "book scanned")
        note(f"server :{args.port} up in {tmp} (db {db_path}); tracks {track_ids}, book {book_id}")

        from audiplex_pc.bus import BusClient
        from audiplex_pc.player import Player
        from audiplex_pc.proxy import AuthProxy

        # Silence first: dummy output, volume 0, checked before anything can play.
        player = Player(vlc_args=["--aout=dummy"], bed_aout=None)
        player.set_volume(0.0)
        if not check(player.volume == 0.0 and player._player.audio_get_volume() <= 0,
                     f"PC player is silent before playback (volume {player.volume}, "
                     f"vlc {player._player.audio_get_volume()}, aout dummy)"):
            return 1
        proxy = AuthProxy(base, pc_token)
        bus = BusClient(base, pc_token, PC_ID, "E2E PC", player, proxy.start())
        bus.start()
        fake = FakeRenderer(base, test_token)
        fake.start()
        phone = FakeRenderer(base, phone_token, device_id=None)
        phone.start()
        _wait(lambda: {PC_ID, TEST_ID, LEGACY_DEVICE_ID}
              <= {d["id"] for d in dj.get("/api/playback/devices").json()["devices"]},
              30, "all three devices registered")

        # (p) phone-first: the phone is mid-track after a seek, nothing declared active.
        check(dj.get("/api/playback/devices").json()["active_device_id"] is None,
              "(p) no device declared active: the phone is the default renderer")
        phone.play(track_ids, PHONE_SEEK_MS)
        time.sleep(2)
        if not args.no_transfer:
            check(dj.post(f"/api/playback/devices/{PC_ID}/activate").status_code == 200,
                  "(p) transfer phone -> PC accepted")
        try:
            _wait(lambda: any(c.get("type") == "deactivate" for c in phone.received), 15, "phone deactivated")
            _wait(lambda: player.state()["playing"] and player.state()["position_ms"] > 0, 30, "PC picked up")
            picked_up = True
        except TimeoutError:
            picked_up = False
        phone_pos = phone.position_ms()
        st = player.state()
        check(picked_up and not phone.playing, f"(p) phone paused at {phone_pos} ms on transfer")
        check(picked_up and (st["track"] or {}).get("id") == track_ids[0]
              and [q["id"] for q in st["queue"]] == track_ids,
              f"(p) PC picked up track {(st['track'] or {}).get('id')} with queue "
              f"{[q['id'] for q in st['queue']]} (phone had {track_ids})")
        check(picked_up and abs(st["position_ms"] - phone_pos) <= TOLERANCE_MS,
              f"(p) PC position {st['position_ms']} ms vs phone {phone_pos} ms")
        check(player.volume == 0.0, "still silent after the phone handoff")
        if args.no_transfer:  # red run: still make the PC active so (a)/(b) run as before
            dj.post(f"/api/playback/devices/{PC_ID}/activate")
        _wait(lambda: dj.get("/api/playback/devices").json()["active_device_id"] == PC_ID, 10, "pc active")
        phone_cmds_before_a = len(phone.received)

        # (a) the shared queue plays on the PC
        play_id = dj.post("/api/playback/command",
                          json={"type": "play_now", "payload": {"track_ids": track_ids}}).json().get("id")

        def acked(cid) -> bool:
            cmds = dj.get("/api/playback/commands", params={"limit": 20}).json()
            cmds = cmds if isinstance(cmds, list) else cmds.get("commands", [])
            return any(c.get("id") == cid and c.get("ack_status") == "ok" for c in cmds)

        # (p) left the PC already playing this queue, so wait for THIS play_now's ack.
        check(play_id is not None and _wait(lambda: acked(play_id), 30, "play_now acked"),
              f"(a) play_now #{play_id} acked by the PC")
        _wait(lambda: player.state()["playing"] and player.state()["position_ms"] > 3000, 30, "PC playing")
        st = player.state()
        check(st["track"]["id"] == track_ids[0] and st["queue_length"] == 3,
              f"(a) PC plays the queue: track {st['track']['id']}, {st['queue_length']} queued, pos {st['position_ms']} ms")
        srv = _wait(lambda: (s := dj.get("/api/playback/state").json()).get("playing") and s, 15, "server sees PC")
        check((srv.get("track") or {}).get("id") == track_ids[0], "(a) server's now-playing is the PC's track")
        check(player.volume == 0.0, "still silent while playing")

        # (b) no double play
        check(fake.received == [],
              "(b) test device, polling the whole time, got no commands while the PC was active")
        check(len(phone.received) == phone_cmds_before_a,
              "(b) phone, polling the whole time, got nothing after its deactivate")

        # (c) PC -> test device
        if not args.no_transfer:
            dj.post(f"/api/playback/devices/{TEST_ID}/activate")
        try:
            act = _wait(lambda: next((c for c in fake.received if c.get("type") == "activate"), None), 15,
                        "test device activated")
        except TimeoutError:
            act = None
        pc_pos = player.state()["position_ms"]
        payload = (act or {}).get("payload") or {}
        check(act is not None and not player.state()["playing"], f"(c) PC paused (at {pc_pos} ms) on transfer")
        check(payload.get("track_ids") == track_ids and payload.get("playing") is True,
              f"(c) test device got the queue {payload.get('track_ids')} playing={payload.get('playing')}")
        check(abs(int(payload.get("position_ms") or -99999) - pc_pos) <= TOLERANCE_MS,
              f"(c) resume position {payload.get('position_ms')} ms vs PC {pc_pos} ms")

        # (d) test device -> PC
        time.sleep(4)
        if not args.no_transfer:
            dj.post(f"/api/playback/devices/{PC_ID}/activate")
        try:
            _wait(lambda: any(c.get("type") == "deactivate" for c in fake.received), 15, "test device deactivated")
            _wait(lambda: player.state()["playing"], 15, "PC resumed")
            resumed = True
        except TimeoutError:
            resumed = False
        fake_pos = fake.position_ms()
        time.sleep(1)
        st = player.state()
        check(resumed and not fake.playing, f"(d) test device paused at {fake_pos} ms, PC playing again")
        check(resumed and (st["track"] or {}).get("id") == track_ids[0]
              and fake_pos - TOLERANCE_MS <= st["position_ms"] <= fake_pos + 1000 + TOLERANCE_MS,
              f"(d) PC resumed track {(st['track'] or {}).get('id')} at {st['position_ms']} ms (test device was {fake_pos})")
        check(player.volume == 0.0, "still silent after the handoffs")

        # (k) book leg: the phone is in an audiobook, reported the way the phone does
        # (track None, book-global position, a fresh progress save), then hands it on.
        activates = sum(c.get("type") == "activate" for c in phone.received)
        dj.post(f"/api/playback/devices/{LEGACY_DEVICE_ID}/activate")
        # Wait for the handoff to land, not for playing: the PC reports every 5 s, so
        # right after (d)'s resume its last report can still say paused.
        _wait(lambda: sum(c.get("type") == "activate" for c in phone.received) > activates,
              15, "phone took the music back")
        phone.client.put(f"/api/progress/{book_id}", json={"position_seconds": BOOK_SEEK_MS / 1000})
        phone.play([], BOOK_SEEK_MS)
        time.sleep(2)
        if not args.no_transfer:
            dj.post(f"/api/playback/devices/{PC_ID}/activate")
        try:
            _wait(lambda: (player.state().get("book") or {}).get("id") == book_id
                  and player.state()["playing"] and player.state()["position_ms"] > 0, 30, "PC opened the book")
            book_ok = True
        except TimeoutError:
            book_ok = False
        phone_pos = phone.position_ms()
        st = player.state()
        check(book_ok and not phone.playing, f"(k) phone paused in the book at {phone_pos} ms")
        check(book_ok and abs(st["position_ms"] - phone_pos) <= TOLERANCE_MS,
              f"(k) PC opened book {(st.get('book') or {}).get('id')} at {st['position_ms']} ms "
              f"(phone was {phone_pos})")
        check(player.volume == 0.0, "still silent after the book handoff")

        cmds = dj.get("/api/playback/commands", params={"limit": 20}).json()
        for c in reversed(cmds if isinstance(cmds, list) else cmds.get("commands", [])):
            note(f"  cmd #{c.get('id')} {c.get('type')} -> {c.get('target_device_id') or '-'} "
                 f"{c.get('status')} {c.get('ack_status') or ''} {json.dumps(c.get('payload'))[:100]}")
    finally:
        for stop in (phone and phone.stop, fake and fake.stop, bus and bus.stop, player and player.stop, proxy and proxy.stop):
            if stop:
                stop()
        server.terminate()
        server.wait(10)
        log.close()

    note(f"RESULT: {'PASS' if not failures else 'FAIL'} ({len(failures)} failed)")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
