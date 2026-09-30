"""#2843/#2806 isolated end-to-end: the REAL dj_* MCP tools against a real server.

    C:\\Python311\\python.exe server/tests/dj_e2e_toolkit.py [--port 8199]

Stands up a throwaway server (temp config.yaml + fresh temp DB) on 127.0.0.1:<port>
with a few short silent tracks. A scripted fake renderer "e2e-test-device" is the
ONLY device on the bus; it plays nothing. Its `mode` scripts how it answers a
start command, so each DJ tool verdict can be checked against the truth:
  honest   - reports the asked-for track playing, then acks ok (new phone build)
  silent   - acks ok on receipt and never plays (old phone build, the #2843 lie)
  failing  - acks "failed: player error" at once
  slow     - acks "failed: not playing 8s after load" after 9 s (the new phone's
             honest timeout, which the old 8 s MCP wait never saw)
Refuses to run on :8100, on the live DB, on a busy port, or if the device id could
be the real phone. Exit code 0 = every check passed.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import logging

import httpx

logging.getLogger("httpx").setLevel(logging.WARNING)  # the report, not every request

SERVER = Path(__file__).resolve().parents[1]
REPO = SERVER.parent
LIVE_PORT = 8100
LIVE_DB = (SERVER / "audiplex.db").resolve()
TEST_ID = "e2e-test-device"
TITLES = ["Alpha Song", "Beta Song", "Gamma Song", "Delta Song"]
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(REPO))

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
    if TEST_ID == LEGACY_DEVICE_ID or not TEST_ID.startswith("e2e-"):
        sys.exit(f"refusing: device id {TEST_ID!r} could collide with a real renderer")
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", port)) == 0:
            sys.exit(f"refusing: something is already listening on :{port}")


def _wait(predicate, timeout: float, what: str):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.25)
    raise TimeoutError(what)


def _up(url: str) -> bool:
    try:
        return httpx.get(url, timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


def _tracks(api: httpx.Client) -> list[dict]:
    out: list[dict] = []
    for artist in api.get("/api/music/artists").json():
        out += api.get(f"/api/music/artists/{artist['id']}/tracks").json()
    return out


def _mint(tmp: Path, env: dict, username: str) -> str:
    out = subprocess.run([sys.executable, "-m", "audiplex.create_service_token", "--username", username],
                         cwd=tmp, env=env, capture_output=True, text=True, check=True).stdout
    for line in out.splitlines():
        if "AUDIPLEX_TOKEN=" in line:
            return line.split("AUDIPLEX_TOKEN=", 1)[1].strip()
    raise RuntimeError(f"no token in: {out}")


class FakeRenderer(threading.Thread):
    """The e2e device: never plays audio; answers per `mode` (see module doc)."""

    START = {"play_now", "resume", "queue", "play_next", "play_stream", "activate"}

    def __init__(self, base: str, token: str, catalog: dict[int, str]) -> None:
        super().__init__(daemon=True)
        self.client = httpx.Client(base_url=base, headers={"Authorization": f"Bearer {token}"}, timeout=35)
        self.catalog = catalog
        self.mode = "honest"
        self.received: list[dict] = []
        self.queue: list[int] = []
        self.index = 0
        self.playing = False
        self.stop = threading.Event()

    def report(self) -> None:
        cur = self.queue[self.index] if 0 <= self.index < len(self.queue) else None
        self.client.post("/api/playback/state", params={"device_id": TEST_ID}, json={
            "playing": self.playing,
            "track": {"id": cur, "title": self.catalog.get(cur, "?"), "artist": "E2E Artist"} if cur else None,
            "queue_length": len(self.queue), "queue_index": self.index,
            "queue": [{"index": i, "id": t, "title": self.catalog.get(t, "?"), "artist": "E2E Artist"}
                      for i, t in enumerate(self.queue)],
        })

    def ack(self, cid: int, status: str, detail: str = "") -> None:
        self.client.post(f"/api/playback/command/{cid}/ack", json={"status": status, "detail": detail})

    def apply(self, cmd: dict) -> None:
        typ, p = cmd["type"], cmd.get("payload") or {}
        if typ == "set_crossfade":  # #2806: behaves like the phone, which can't crossfade
            self.crossfade_asked = p.get("seconds")
            self.ack(cmd["id"], "unknown_type", typ)
            return
        ids = list(p.get("track_ids") or [])
        if typ == "play_now":
            self.queue, self.index = ids, 0
        elif typ == "queue":
            self.queue += ids
        elif typ == "play_next":
            self.queue[self.index + 1:self.index + 1] = ids
        elif typ == "replace_upcoming":
            self.queue = self.queue[:self.index + 1] + ids
        elif typ == "pause":
            self.playing = False
        if typ not in self.START:
            self.report()
            self.ack(cmd["id"], "ok")
            return
        if self.mode == "honest":
            self.playing = bool(self.queue) or typ == "activate"
            self.report()
            self.ack(cmd["id"], "ok")
        elif self.mode == "silent":
            self.ack(cmd["id"], "ok")
            self.playing = False
            self.report()
        elif self.mode == "failing":
            self.playing = False
            self.report()
            self.ack(cmd["id"], "failed", "player error: HTTP 404")
        elif self.mode == "slow":
            self.playing = False
            self.report()
            time.sleep(9.0)
            self.ack(cmd["id"], "failed", "not playing 8s after load")

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                r = self.client.get("/api/playback/command/next", params={
                    "device_id": TEST_ID, "device_name": "E2E Test Device", "device_type": "test"})
            except httpx.HTTPError:
                continue
            if r.status_code != 200:
                continue
            cmd = r.json()
            self.received.append(cmd)
            try:
                self.apply(cmd)
            except httpx.HTTPError as exc:
                note(f"fake renderer: {exc!r}")


def first(out: str) -> str:
    return out.split("\n", 1)[0]


async def verdict_checks(dj, fake: FakeRenderer, ids: list[int]) -> None:
    """#2843: a DJ play is only reported as playing when it really started."""
    fake.mode = "honest"
    out = await dj.dj_play_now(ids[:2])
    check("playing=YES" in first(out) and "PLAYING=" not in out,
          f"honest device: dj_play_now says playing=YES ({first(out)})")

    out = await dj.dj_queue(ids[2:3])
    check("playing=YES" in first(out), f"honest device: dj_queue on a live player is YES ({first(out)})")

    fake.mode = "silent"
    started = time.monotonic()
    out = await dj.dj_play_now(ids[1:3])
    took = time.monotonic() - started
    check("playing=YES" not in first(out) and "Do NOT tell Todd it's playing" in out
          and "should pick this up" not in out,
          f"old build acks ok but stays silent: NOT YES ({first(out)})")
    check(took <= dj.START_VERIFY_S + 3, f"the verdict wait is capped ({took:.1f}s)")

    fake.mode = "failing"
    started = time.monotonic()
    out = await dj.dj_play_now(ids[:1])
    took = time.monotonic() - started
    check("playing=NO" in first(out) and "player error: HTTP 404" in out,
          f"a failed ack is playing=NO with the reason ({first(out)})")
    check(took < 5, f"a failed ack returns early ({took:.1f}s)")

    fake.mode = "slow"
    out = await dj.dj_play_now(ids[:1])
    check("playing=NO" in first(out) and "not playing 8s after load" in out,
          f"the phone's honest 9 s failure is seen, not raced ({first(out)})")

    fake.mode = "failing"
    out = await dj.dj_resume()
    check("playing=NO" in first(out), f"dj_resume carries the verdict ({first(out)})")

    # Jarvis rider 2: dj_pool_set's start path gets the verdict too.
    fake.mode = "honest"
    await dj.dj_pause()
    fake.queue, fake.index, fake.playing = [], 0, False
    fake.report()
    out = await dj.dj_pool_set(sources=[{"kind": "artist", "query": "E2E Artist"}], ahead=2,
                               exclude_recent_hours=0)
    check("playing=YES" in out and "pool" in out.lower(),
          f"dj_pool_set on an idle player reports a verdict: {out[:160]!r}")
    await dj.dj_pool_stop()

    only = {c.get("target_device_id") for c in await dj._get("/api/playback/commands?limit=200")}
    check(only <= {None, TEST_ID}, f"no command ever targeted another device: {only}")


async def toolkit_checks(dj, fake: FakeRenderer, ids: list[int]) -> None:  # #2806 S1
    from audiplex_mcp import dj_toolkit as tk

    fake.mode = "honest"
    await dj.dj_play_now(ids)
    a, b, c, d = ids[:4]
    out = await tk.dj_upcoming()
    check(f">#0  id {a}" in out and "3 track(s) after" in out, f"dj_upcoming shows the queue: {out[:120]!r}")
    out = await tk.dj_remove(indexes=[2])
    check(fake.queue == [a, b, d] and fake.index == 0, f"dj_remove #2 -> {fake.queue} ({out[:80]!r})")
    await tk.dj_insert([c], at_index=1)
    check(fake.queue == [a, c, b, d], f"dj_insert at #1 -> {fake.queue}")
    await tk.dj_swap(3, [a])
    check(fake.queue == [a, c, b, a], f"dj_swap #3 -> {fake.queue}")
    before = list(fake.queue)
    out = await tk.dj_remove(indexes=[0])
    check(fake.queue == before and "playing now" in out, "an edit never touches the current song")
    out = await tk.dj_ban([b], reason="e2e")
    check(b not in fake.queue[1:] and "Banned 1" in out, f"dj_ban drops it from the queue: {fake.queue}")
    plan = await dj._post("/api/playback/mix/plan", {"new_ids": ids, "shuffle": False})
    check(b not in plan["upcoming"], f"a banned track is not planned into a mix: {plan['upcoming']}")
    out = await tk.dj_bans()
    check(f"id {b}" in out, "dj_bans lists it")
    await tk.dj_unban([b])
    plan = await dj._post("/api/playback/mix/plan", {"new_ids": ids, "shuffle": False})
    check(b in plan["upcoming"] or b == a, f"dj_unban makes it plannable again: {plan['upcoming']}")
    await dj.dj_pause()
    fake.queue, fake.index, fake.playing = [], 0, False
    fake.report()
    await dj.dj_pool_set(sources=[{"kind": "search", "query": "alpha"}, {"kind": "search", "query": "beta"}],
                         ahead=2, exclude_recent_hours=0)
    out = await tk.dj_pool_lane("beta", "pause")
    check("(paused)" in out, f"dj_pool_lane pauses a lane: {out!r}")
    st = await dj._get("/api/playback/pool")
    check(any(ln.get("paused") for ln in st["lanes"]), "the server pool reports the paused lane")
    out = await dj.dj_pool_set(sources=[{"kind": "vibes", "query": "x"}])
    check("REFUSED" in out and "'tag', or 'tracks'" in out, f"an unknown kind names the valid ones: {out[:160]!r}")  # #2806
    await dj.dj_pool_stop()


def analyzer_checks(fake: FakeRenderer, base: str, token: str, db_path: Path, tmp: Path) -> None:  # #2806 S2
    """The real energy analyzer against the throwaway server: busy while playing, runs when idle."""
    script = SERVER / "scripts" / "measure_energy.py"
    common = [sys.executable, str(script), "--db", str(db_path), "--all-tracks", "--url", base,
              "--token", token, "--bike-state", str(tmp / "no-bike.json"), "--sleep", "0",
              "--report", str(tmp / "energy.json")]
    fake.queue, fake.index, fake.playing = [1], 0, True
    fake.report()
    r = subprocess.run(common, capture_output=True, text=True)
    check(r.returncode == 3 and "music is playing" in r.stderr, f"analyzer refuses while playing ({r.returncode} {r.stderr.strip()!r})")
    (tmp / "ride.json").write_text('{"active": true}', encoding="utf-8")
    fake.playing = False
    fake.report()
    r = subprocess.run(common[:-6] + ["--bike-state", str(tmp / "ride.json")] + common[-4:], capture_output=True, text=True)
    check(r.returncode == 3 and "bike ride" in r.stderr, f"analyzer refuses during a ride ({r.stderr.strip()!r})")
    r = subprocess.run(common, capture_output=True, text=True)
    rep = json.loads((tmp / "energy.json").read_text(encoding="utf-8")) if r.returncode == 0 else {}
    check(r.returncode == 0 and len(rep.get("failed", [])) == len(TITLES),
          f"analyzer runs when idle; silent e2e tracks are reported, not scored ({r.stdout.strip()[:120]!r})")


async def energy_checks(dj, fake: FakeRenderer, ids: list[int], db_path: Path) -> None:  # #2806 S2
    import sqlite3

    from audiplex_mcp import dj_toolkit as tk

    energy = dict(zip(ids, (70, 20, 90, 40)))
    con = sqlite3.connect(db_path)
    with con:
        con.executemany("UPDATE tracks SET energy=? WHERE id=?", [(e, i) for i, e in energy.items()])
    con.close()
    out = await tk.dj_tag(ids, ["e2e ride"])
    check(f"Tagged {len(ids)} track(s)" in out, f"dj_tag: {out!r}")
    out = await tk.dj_tags()
    check("e2e ride (4)" in out, f"dj_tags lists the tag: {out!r}")
    fake.mode = "honest"
    await dj.dj_play_now([ids[0]])
    cur = fake.queue[fake.index]
    out = await tk.dj_energy_set("wind_down", sources=[{"kind": "tag", "query": "e2e ride"}], exclude_recent_hours=0)
    tail = [energy[i] for i in fake.queue[fake.index + 1:]]
    check(fake.queue[fake.index] == cur and tail and tail == sorted(tail, reverse=True),
          f"dj_energy_set wind_down queues high->low after the current song: {tail} ({out[:140]!r})")
    out = await tk.dj_energy_set("rise", sources=[{"kind": "tag", "query": "e2e ride"}], exclude_recent_hours=0)
    tail = [energy[i] for i in fake.queue[fake.index + 1:]]
    check(tail and tail == sorted(tail), f"a second set REPLACES the tail, low->high: {tail}")
    out = await tk.dj_untag(ids, ["e2e ride"])
    out = await tk.dj_energy_set("rise", sources=[{"kind": "tag", "query": "e2e ride"}])
    check(out.startswith("REFUSED"), f"an empty tag source refuses and sends nothing: {out[:100]!r}")
    out = await tk.dj_crossfade(20)  # #2806 S3: over the real bus to a phone-like device
    check(getattr(fake, "crossfade_asked", None) == 12.0 and "can't crossfade" in out,
          f"dj_crossfade clamps to 12 s, and a phone-like device's refusal is said plainly: {out!r}")
    await dj.dj_pause()


def tempo_key_analyzer_checks(fake: FakeRenderer, base: str, token: str, db_path: Path, tmp: Path) -> None:  # #1002
    """The real tempo/key batch against the throwaway server: shares measure_energy's guard."""
    script = SERVER / "scripts" / "measure_tempo_key.py"
    common = [sys.executable, str(script), "--db", str(db_path), "--all-tracks", "--url", base,
              "--token", token, "--bike-state", str(tmp / "no-bike.json"), "--sleep", "0",
              "--report", str(tmp / "tempo-key.json")]
    fake.queue, fake.index, fake.playing = [1], 0, True
    fake.report()
    r = subprocess.run(common, capture_output=True, text=True)
    check(r.returncode == 3 and "music is playing" in r.stderr, f"tempo/key batch refuses while playing ({r.returncode} {r.stderr.strip()!r})")
    r = subprocess.run(common[:-6] + ["--bike-state", str(tmp / "ride.json")] + common[-4:], capture_output=True, text=True)
    check(r.returncode == 3 and "bike ride" in r.stderr, f"tempo/key batch refuses during a ride ({r.stderr.strip()!r})")
    fake.playing = False
    fake.report()
    r = subprocess.run(common[:5] + ["--url", "http://127.0.0.1:9"] + common[7:], capture_output=True, text=True)
    check(r.returncode == 3 and "can't verify" in r.stderr, f"tempo/key batch fails closed when the player can't be checked ({r.stderr.strip()!r})")
    r = subprocess.run(common, capture_output=True, text=True)
    rep = json.loads((tmp / "tempo-key.json").read_text(encoding="utf-8")) if r.returncode == 0 else {}
    check(r.returncode == 0 and rep.get("checked") == len(TITLES) and not rep.get("applied"),
          f"tempo/key batch runs when idle, report-only by default ({r.stdout.strip()[:120]!r})")


async def harmonic_checks(dj, fake: FakeRenderer, ids: list[int], db_path: Path) -> None:  # #1002
    import sqlite3

    from audiplex.harmonic import key_score
    from audiplex_mcp import dj_toolkit as tk

    meta = dict(zip(ids, ((128.0, "8A"), (126.0, "3B"), (127.0, "9B"), (129.0, "8B"))))
    con = sqlite3.connect(db_path)
    with con:
        con.executemany("UPDATE tracks SET bpm=?, musical_key=? WHERE id=?", [(b, k, i) for i, (b, k) in meta.items()])
    con.close()
    fake.mode = "honest"
    await dj.dj_play_now([ids[0]])
    cur = fake.queue[fake.index]
    out = await tk.dj_harmonic_set(track_ids=ids[1:], start_track_id=ids[3], exclude_recent_hours=0)
    tail = fake.queue[fake.index + 1:]
    keys = [meta[i][1] for i in tail]
    check(fake.queue[fake.index] == cur and tail[:2] == [ids[3], ids[2]] and "Harmonic set: 3 track(s)" in out
          and key_score(keys[0], keys[1]) >= 0.9,
          f"dj_harmonic_set queues key-compatible hand-offs after the current song: {keys} ({out[:160]!r})")
    await dj.dj_pause()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8199)
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="audiplex-e2e-dj-"))
    db_path = tmp / "audiplex.db"
    _guard(args.port, db_path)
    base = f"http://127.0.0.1:{args.port}"

    album = tmp / "lib" / "music" / "E2E Artist" / "E2E Album"
    album.mkdir(parents=True)
    for n, title in enumerate(TITLES, 1):
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                        "-c:a", "aac", "-t", "5", "-metadata", f"title={title}", "-metadata", "artist=E2E Artist",
                        "-metadata", "album=E2E Album", "-metadata", f"track={n}",
                        str(album / f"0{n} track.m4a")], check=True)
    (tmp / "config.yaml").write_text(json.dumps({
        "library_roots": [{"path": str(tmp / "lib" / "music"), "category": "music"}],
        "database_url": f"sqlite:///{db_path.as_posix()}",
        "port": args.port,
        "cover_cache_dir": str(tmp / "covers"),
        "dj_clip_dir": str(tmp / "dj_clips"),
        "jwt_secret": "e2e-dj-" + "0" * 40,
        "dj_owner_username": "owner",  # #2806: mix/plan resolves the owner
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
    fake = None
    try:
        _wait(lambda: _up(f"{base}/api/health"), 60, "server up")
        owner = httpx.post(f"{base}/api/auth/register",
                           json={"username": "owner", "password": "e2e-pass"}).json()["token"]
        api = httpx.Client(base_url=base, headers={"Authorization": f"Bearer {owner}"}, timeout=30)
        api.post("/api/library/scan")
        tracks = _wait(lambda: len(t := _tracks(api)) >= len(TITLES) and t, 60, "tracks scanned")
        catalog = {t["id"]: t["title"] for t in tracks}
        ids = sorted(catalog)
        note(f"server :{args.port} up in {tmp}; tracks {catalog}")

        fake = FakeRenderer(base, _mint(tmp, env, TEST_ID), catalog)
        fake.start()
        _wait(lambda: any(d["id"] == TEST_ID for d in api.get("/api/playback/devices").json()["devices"]),
              30, "e2e device registered")
        api.post(f"/api/playback/devices/{TEST_ID}/activate")
        devs = {d["id"] for d in api.get("/api/playback/devices").json()["devices"]}
        if not check(devs == {TEST_ID}, f"only e2e devices on the bus: {sorted(devs)}"):
            return 1

        # The REAL MCP module, pointed at this throwaway server only. The talk
        # guard reads a missing speech file (unguarded) and the announce gate,
        # which reads Pantheon's chat DB, is bypassed: neither is under test.
        os.environ["DJ_SPEECH_STATE_FILE"] = str(tmp / "no-speech-state.json")
        os.environ["DJ_MIX_SOURCES_FILE"] = str(tmp / "mix-sources.json")  # #2806: not the live labels
        from audiplex_mcp import server as dj
        dj.AUDIPLEX_URL, dj.AUDIPLEX_TOKEN = base, owner

        async def no_gate(cmd_type):
            return None

        dj._announce_gate = no_gate
        asyncio.run(verdict_checks(dj, fake, ids))
        asyncio.run(toolkit_checks(dj, fake, ids))  # #2806
        analyzer_checks(fake, base, owner, db_path, tmp)  # #2806 S2
        asyncio.run(energy_checks(dj, fake, ids, db_path))  # #2806 S2
        tempo_key_analyzer_checks(fake, base, owner, db_path, tmp)  # #1002
        asyncio.run(harmonic_checks(dj, fake, ids, db_path))  # #1002
    finally:
        if fake:
            fake.stop.set()
        server.terminate()
        server.wait(10)
        log.close()

    note(f"RESULT: {'PASS' if not failures else 'FAIL'} ({len(failures)} failed)")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
