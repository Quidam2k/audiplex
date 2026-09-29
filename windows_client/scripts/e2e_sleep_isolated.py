"""#3435 isolated end-to-end: dj_sleep_start with the PC renderer as target.

    C:\\Python311\\python.exe windows_client/scripts/e2e_sleep_isolated.py [--port 8199]

Stands up a throwaway server (temp config.yaml/DB/logs) on 127.0.0.1:<port>
whose temp library holds two DIGITALLY SILENT files: a music track and a bed
titled "Brown Noise - Sleep Loop" so the default-bed lookup finds it. Runs the
REAL PC Player/BusClient/AuthProxy against it on VLC's real outputs (mmdevice
main, DirectSound bed) so the volume readbacks are genuine; --aout=dummy reads
every volume as 0. The content is silence, so nothing is heard. Makes the PC the
active device, starts the track, calls the real MCP dj_sleep_start with no
arguments, samples both players every 250 ms and writes a timeline. Also
reports the real brown-noise loop's peak/mean level. Never touches :8100, the
phone or a running PC renderer.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import logging

logging.getLogger("httpx").setLevel(logging.WARNING)

REPO = Path(__file__).resolve().parents[2]
SERVER = REPO / "server"
BED_SRC = Path("Q:/meditations/audio/Brown Noise - Sleep Loop.m4a")
sys.path.insert(0, str(REPO / "windows_client"))
sys.path.insert(0, str(REPO))


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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8199)
    ap.add_argument("--after-min", type=float, default=0.1)  # 6 s of untouched play
    ap.add_argument("--fade-s", type=int, default=5)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()
    if args.port == 8100:
        sys.exit("refusing to run against the live port")

    tmp = Path(tempfile.mkdtemp(prefix="audiplex-e2e-3435-"))
    base = f"http://127.0.0.1:{args.port}"
    music = tmp / "lib" / "music" / "E2E Artist" / "E2E Album"
    med = tmp / "lib" / "meditation"
    music.mkdir(parents=True)
    med.mkdir(parents=True)
    silence = ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
               "anullsrc=r=44100:cl=stereo", "-c:a", "aac"]
    subprocess.run(silence + ["-t", "120", "-metadata", "title=E2E Silence",
                              "-metadata", "artist=E2E Artist", "-metadata", "album=E2E Album",
                              "-metadata", "track=1", str(music / "01 E2E Silence.m4a")], check=True)
    subprocess.run(silence + ["-t", "20", "-metadata", "title=Brown Noise - Sleep Loop",
                              str(med / "Brown Noise - Sleep Loop.m4a")], check=True)
    # Loop check: the REAL bed, played muted at volume 0 and seeked near its end.
    # (A synthetic silent AAC is only a few KB and VLC stalls on it over HTTP.)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(BED_SRC), "-c", "copy",
                    "-metadata", "title=E2E Loop Check", str(med / "E2E Loop Check.m4a")], check=True)
    (tmp / "config.yaml").write_text(json.dumps({
        "library_roots": [
            {"path": str(tmp / "lib" / "music"), "category": "music"},
            {"path": str(med), "category": "meditation"},
        ],
        "database_url": f"sqlite:///{(tmp / 'audiplex.db').as_posix()}",
        "port": args.port,
        "cover_cache_dir": str(tmp / "covers"),
        "dj_clip_dir": str(tmp / "dj_clips"),
        "jwt_secret": "e2e-3435-" + "0" * 40,
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
    timeline: list[dict] = []
    events: list[str] = []
    t0 = time.monotonic()

    def note(msg: str) -> None:
        line = f"[{time.monotonic() - t0:6.2f}s] {msg}"
        events.append(line)
        print(line, flush=True)

    bus = proxy = player = None
    try:
        _wait(lambda: _ok(f"{base}/docs"), 60, "server up")
        dj_token = _mint(tmp, env, None)
        pc_token = _mint(tmp, env, "pc-e2e")
        dj = httpx.Client(base_url=base, headers={"Authorization": f"Bearer {dj_token}"}, timeout=30)
        dj.post("/api/library/scan")
        books = _wait(lambda: [b for b in dj.get("/api/library/books").json()
                               if "Brown Noise" in (b.get("title") or "")], 60, "bed scanned")
        tracks = _wait(lambda: _tracks(dj), 60, "track scanned")
        note(f"server :{args.port} up in {tmp}; bed book {books[0]['id']}, track {tracks[0]}")

        from audiplex_pc.bus import BusClient
        from audiplex_pc.player import Player
        from audiplex_pc.proxy import AuthProxy

        proxy = AuthProxy(base, pc_token)
        proxy_base = proxy.start()
        player = Player()
        bus = BusClient(base, pc_token, "pc-e2e", "E2E PC", player, proxy_base)
        bus.start()
        _wait(lambda: any(d.get("id") == "pc-e2e" for d in _devices(dj)), 30, "pc registered")
        assert dj.post("/api/playback/devices/pc-e2e/activate").status_code == 200
        note("PC renderer registered and activated (real VLC outputs, silent content)")

        os.environ.update(AUDIPLEX_URL=base, AUDIPLEX_TOKEN=dj_token,
                          DJ_SPEECH_STATE_FILE=str(tmp / "no-speech-state.json"))
        os.environ.pop("AUDIPLEX_SLEEP_BED_URL", None)
        mcp = importlib.import_module("audiplex_mcp.server")
        # Setup only (not under test): a raw play_now, past the DJ's announce-first gate.
        queued = dj.post("/api/playback/command",
                         json={"type": "play_now", "payload": {"track_ids": [tracks[0]]}}).json()
        note(f"setup play_now queued as cmd #{queued.get('id')}")
        _wait(lambda: player.state()["playing"] and player.state()["position_ms"] > 0, 30, "track playing")
        main_mp = player._player
        mrl0 = main_mp.get_media().get_mrl()
        _wait(lambda: dj.get("/api/playback/state").json().get("playing"), 30, "server sees PC playing")
        note(f"server sees the PC playing; main mrl {mrl0}")
        note("dj_sleep_start() -> " + " | ".join(
            asyncio.run(mcp.dj_sleep_start(fade_after_minutes=args.after_min,
                                           fade_seconds=args.fade_s)).splitlines()))

        end = time.monotonic() + args.after_min * 60 + args.fade_s + 8
        while time.monotonic() < end:
            bed = player._bed
            timeline.append({
                "t": round(time.monotonic() - t0, 2),
                "main_vol": main_mp.audio_get_volume(),
                "main_state": str(main_mp.get_state()).split(".")[-1],
                "main_pos_ms": main_mp.get_time(),
                "main_mrl_same": main_mp.get_media().get_mrl() == mrl0,
                "bed_vol": bed.audio_get_volume() if bed else None,
                "bed_level": round(player.bed_level, 3),
                "bed_state": str(bed.get_state()).split(".")[-1] if bed else None,
                "bed_pos_ms": bed.get_time() if bed else None,
                "timer_alive": player.sleep_timer_active(),
            })
            time.sleep(0.25)
        acks = dj.get("/api/playback/commands", params={"limit": 20}).json()
        for c in reversed(acks if isinstance(acks, list) else acks.get("commands", [])):
            note(f"cmd #{c.get('id')} {c.get('type')} -> {c.get('status')} "
                 f"{c.get('ack_status') or ''} {json.dumps(c.get('payload'))[:120]}")
        loop_check(dj, player, proxy_base, note)
    finally:
        if bus:
            bus.stop()
        if player:
            player.cancel_sleep_timer()
            player.bed_stop()
            player.stop()
        if proxy:
            proxy.stop()
        server.terminate()
        server.wait(10)
        log.close()

    level = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(BED_SRC), "-af", "volumedetect",
                            "-f", "null", "-"], capture_output=True, text=True).stderr
    for line in level.splitlines():
        if "max_volume" in line or "mean_volume" in line:
            note("real bed " + line.split("]")[-1].strip())

    out = args.out or tmp / "timeline.json"
    out.write_text(json.dumps({"events": events, "timeline": timeline}, indent=1), encoding="utf-8")
    print(f"timeline: {out}")
    for row in timeline[:: max(len(timeline) // 40, 1)]:
        print(row)
    return 0


def loop_check(dj: httpx.Client, player, proxy_base: str, note) -> None:
    """The bed must wrap and keep playing at the end of its file."""
    books = [b for b in dj.get("/api/library/books").json() if b.get("title") == "E2E Loop Check"]
    if not books:
        note("loop check: SKIPPED (loop-check book not scanned)")
        return
    player.cancel_sleep_timer()
    player.bed_play(f"{proxy_base}/api/stream/{books[0]['id']}", 0.0)
    bed = player._bed
    bed.audio_set_mute(True)
    _wait(lambda: bed.get_length() > 0 and bed.get_time() > 0, 30, "loop bed playing")
    length = bed.get_length()
    bed.set_time(length - 3000)
    samples = []
    for _ in range(32):
        time.sleep(0.25)
        samples.append(bed.get_time())
    wrapped = any(b < a - 1000 for a, b in zip(samples, samples[1:]))
    note(f"loop check: len {length} ms, pos {samples[0]}..{max(samples)} then {samples[-1]}; "
         f"wrapped={wrapped} state={str(bed.get_state()).split('.')[-1]} same_player={bed is player._bed}")


def _ok(url: str) -> bool:
    try:
        return httpx.get(url, timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


def _tracks(client: httpx.Client) -> list[int]:
    ids: list[int] = []
    for artist in client.get("/api/music/artists").json():
        ids += [t["id"] for t in client.get(f"/api/music/artists/{artist['id']}/tracks").json()]
    return ids


def _devices(client: httpx.Client) -> list[dict]:
    data = client.get("/api/playback/devices").json()
    return data.get("devices", []) if isinstance(data, dict) else data


if __name__ == "__main__":
    sys.exit(main())
