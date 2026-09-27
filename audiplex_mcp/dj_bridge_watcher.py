"""Long-running watcher: reads Audiplex now-playing (read-only, never sends
playback commands) and, every 2nd-3rd music-track change, shells out to a
hook so a voice persona can speak a DJ bridge.

Lives at Q:\\Development\\audiplex\\audiplex_mcp\\dj_bridge_watcher.py
Module: audiplex_mcp.dj_bridge_watcher
"""
import argparse
import json
import os
import random
import shlex
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
POLL_SECONDS = 2.0
DEFAULT_SETTINGS = {"on": False, "every_min": 2, "every_max": 3}


# --------------------------------------------------------------------------
# Config (env-driven, read fresh where cheap so on/off toggles apply live)
# --------------------------------------------------------------------------

def audiplex_url():
    return os.environ.get("AUDIPLEX_URL", "http://localhost:8100")


def audiplex_token():
    tok = os.environ.get("AUDIPLEX_TOKEN")
    if tok:
        return tok.strip()
    token_file = REPO_ROOT / ".dj_token"
    if token_file.exists():
        return token_file.read_text(encoding="utf-8").strip()
    return ""


def bridge_cmd_template():
    return os.environ.get(
        "DJ_BRIDGE_CMD",
        "Q:/Pantheon/.venv-omnivoice/Scripts/python.exe "
        "Q:/Pantheon/scripts/dj_bridge_push.py --payload-file {json_file}",
    )


def patter_file():
    return Path(os.environ.get("DJ_PATTER_FILE", str(REPO_ROOT / "data" / "dj_patter.json")))


def log_file():
    return Path(os.environ.get("DJ_BRIDGE_LOG", str(REPO_ROOT / "data" / "dj_bridge.log")))


def log_line(message):
    ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    line = f"{ts} {message}"
    print(line, flush=True)
    try:
        lf = log_file()
        lf.parent.mkdir(parents=True, exist_ok=True)
        with lf.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass  # logging must never crash the loop


def load_settings():
    try:
        raw = json.loads(patter_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict(DEFAULT_SETTINGS)
    if not isinstance(raw, dict):
        return dict(DEFAULT_SETTINGS)
    try:
        every_min = max(1, int(raw.get("every_min", 2)))
    except (TypeError, ValueError):
        every_min = 2
    try:
        every_max = int(raw.get("every_max", 3))
    except (TypeError, ValueError):
        every_max = 3
    every_max = max(every_min, every_max)
    return {"on": bool(raw.get("on", False)), "every_min": every_min, "every_max": every_max}


def write_settings(settings):
    pf = patter_file()
    pf.parent.mkdir(parents=True, exist_ok=True)
    tmp = pf.with_suffix(pf.suffix + ".tmp")
    tmp.write_text(json.dumps(settings), encoding="utf-8")
    os.replace(tmp, pf)


# --------------------------------------------------------------------------
# Pure, unit-testable transition/counter logic
# --------------------------------------------------------------------------

class BridgeCounter:
    """Decides when a DJ bridge should fire. No I/O, no globals - takes a
    state dict + settings dict + timestamp, returns a payload or None."""

    def __init__(self):
        self.prev_music = None     # {"id","title","artist"} of the prior music track
        self.last_music = None     # most recently seen music track
        self.counter = 0
        self.target = None
        self._bounds = None
        self.last_skip_reason = None  # informational only, for the run loop's logging

    @staticmethod
    def _clamp(settings):
        every_min = max(1, int(settings.get("every_min", 2)))
        every_max = max(every_min, int(settings.get("every_max", 3)))
        return every_min, every_max

    def _maybe_redraw(self, settings):
        bounds = self._clamp(settings)
        if self.target is None or bounds != self._bounds:
            self.target = random.randint(bounds[0], bounds[1])
            self._bounds = bounds

    def observe(self, state, settings, now):
        self.last_skip_reason = None
        self._maybe_redraw(settings)

        track = state.get("track")
        playing = bool(state.get("playing"))

        # DJ clips / streams (id <= 0 or missing) don't exist for this class.
        if not track or not isinstance(track.get("id"), int) or track.get("id", 0) <= 0:
            self.last_skip_reason = "not_music"
            return None

        if not playing:
            self.last_skip_reason = "not_playing"
            return None

        tid = track["id"]

        if self.last_music is None:
            # First music track since startup: initialize only, never counts.
            self.last_music = {"id": tid, "title": track.get("title"), "artist": track.get("artist")}
            self.last_skip_reason = "init"
            return None

        if tid == self.last_music.get("id"):
            # Same track: pause/resume/seek. Not a transition.
            self.last_skip_reason = "same_track"
            return None

        # --- transition ---
        self.prev_music = self.last_music
        self.last_music = {"id": tid, "title": track.get("title"), "artist": track.get("artist")}

        if not bool(settings.get("on", False)):
            self.counter = 0
            self.last_skip_reason = "off"
            return None

        position_s = (state.get("position_ms") or 0) / 1000.0
        duration_s = (state.get("duration_ms") or 0) / 1000.0

        if position_s > 25:
            # Detected too late (e.g. watcher was down) - reset but don't fire.
            self.counter = 0
            self.last_skip_reason = "stale"
            return None

        self.counter += 1
        if self.counter < self.target:
            self.last_skip_reason = "counting"
            return None

        # --- fire ---
        self.counter = 0
        self._bounds = None
        self.target = None
        self._maybe_redraw(settings)  # redraw target after every fire

        next_item = None
        queue_index = state.get("queue_index")
        if isinstance(queue_index, int):
            for q in state.get("queue") or []:
                if q.get("index") == queue_index + 1:
                    next_item = {"title": q.get("title"), "artist": q.get("artist")}
                    break

        prev_payload = None
        if self.prev_music:
            prev_payload = {"title": self.prev_music.get("title"), "artist": self.prev_music.get("artist")}

        detected_dt = datetime.fromtimestamp(now, tz=timezone.utc)
        return {
            "prev": prev_payload,
            "now": {
                "title": track.get("title"),
                "artist": track.get("artist"),
                "position_s": position_s,
                "duration_s": duration_s,
            },
            "next": next_item,
            "detected_at": detected_dt.isoformat().replace("+00:00", "Z"),
            "detected_epoch": now,
            "source": "audiplex",
        }


# --------------------------------------------------------------------------
# I/O: HTTP fetch, hook invocation
# --------------------------------------------------------------------------

def fetch_state(client, base_url, token):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    resp = client.get(f"{base_url}/api/playback/state", headers=headers, timeout=10.0)
    resp.raise_for_status()
    return resp.json()


def build_argv(json_file_path):
    parts = shlex.split(bridge_cmd_template(), posix=False)
    argv = []
    for part in parts:
        part = part.replace("{json_file}", str(json_file_path))
        if len(part) >= 2 and part[0] == '"' and part[-1] == '"':
            part = part[1:-1]
        argv.append(part)
    return argv


def fire_hook(payload):
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    tmp_path = Path(path)
    try:
        tmp_path.write_text(json.dumps(payload), encoding="utf-8")
        argv = build_argv(tmp_path)
        try:
            result = subprocess.run(argv, timeout=30, capture_output=True, text=True)
            log_line(
                f"hook rc={result.returncode} "
                f"stdout={result.stdout[:300]!r} stderr={result.stderr[:300]!r}"
            )
        except Exception as exc:  # a hook failure must never crash the loop
            log_line(f"hook failed: {exc!r}")
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


# --------------------------------------------------------------------------
# Run loop / CLI
# --------------------------------------------------------------------------

def run_loop():
    base_url = audiplex_url()
    token = audiplex_token()
    counter = BridgeCounter()
    last_error = None
    log_line("dj_bridge_watcher starting")
    with httpx.Client() as client:
        while True:
            settings = load_settings()
            try:
                state = fetch_state(client, base_url, token)
            except httpx.HTTPError as exc:
                msg = str(exc)
                if msg != last_error:
                    log_line(f"state fetch error: {msg}")
                    last_error = msg
                time.sleep(POLL_SECONDS)
                continue
            last_error = None

            payload = counter.observe(state, settings, time.time())
            if counter.last_skip_reason == "stale":
                log_line("stale")
            if payload is not None:
                fire_hook(payload)

            time.sleep(POLL_SECONDS)


def run_once_dry():
    base_url = audiplex_url()
    token = audiplex_token()
    settings = load_settings()
    counter = BridgeCounter()
    with httpx.Client() as client:
        try:
            state = fetch_state(client, base_url, token)
        except httpx.HTTPError as exc:
            print(f"state fetch error: {exc}")
            return
    payload = counter.observe(state, settings, time.time())
    print(f"settings={settings}")
    print(f"skip_reason={counter.last_skip_reason}")
    print(f"payload={json.dumps(payload) if payload else None}")


def cmd_status():
    print(f"settings: {json.dumps(load_settings())}")
    lf = log_file()
    lines = lf.read_text(encoding="utf-8").splitlines()[-5:] if lf.exists() else []
    print("last 5 log lines:")
    for line in lines:
        print(line)


def cmd_on(every_min, every_max):
    settings = load_settings()
    if every_min is not None:
        settings["every_min"] = max(1, every_min)
    if every_max is not None:
        settings["every_max"] = every_max
    settings["every_max"] = max(settings["every_min"], settings["every_max"])
    settings["on"] = True
    write_settings(settings)
    print(f"patter on: {json.dumps(settings)}")


def cmd_off():
    settings = load_settings()
    settings["on"] = False
    write_settings(settings)
    print(f"patter off: {json.dumps(settings)}")


def main():
    parser = argparse.ArgumentParser(prog="dj_bridge_watcher")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run")
    p_run.add_argument("--once-dry", action="store_true")

    p_on = sub.add_parser("on")
    p_on.add_argument("--every-min", type=int, default=None)
    p_on.add_argument("--every-max", type=int, default=None)

    sub.add_parser("off")
    sub.add_parser("status")

    args = parser.parse_args()

    if args.cmd == "run":
        run_once_dry() if args.once_dry else run_loop()
    elif args.cmd == "on":
        cmd_on(args.every_min, args.every_max)
    elif args.cmd == "off":
        cmd_off()
    elif args.cmd == "status":
        cmd_status()


if __name__ == "__main__":
    main()
