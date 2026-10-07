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
DEFAULT_SETTINGS = {"on": False, "every_min": 3, "every_max": 3}  # #5986: one bridge per 3 songs
OUTRO_LEAD_S = 20.0  # #5986: fire this long before the armed song ends, so the talk spans the fade
FACTS_TIMEOUT_S = 3.0  # #5986


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
        every_min = max(1, int(raw.get("every_min", 3)))  # #5986
    except (TypeError, ValueError):
        every_min = 3  # #5986
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
    state dict + settings dict + timestamp, returns a payload or None.

    #5986: every `target` (3) track changes, the new track is ARMED and the
    bridge fires ~OUTRO_LEAD_S before it ENDS (mode 'outro': now = the
    outgoing song, next = the queue's next), so the persona talks across the
    fade. If the armed song is skipped before that, or its duration is
    unknown, the bridge fires at the start of the following song instead
    (mode 'intro', the pre-#5986 behaviour). Either way exactly one bridge,
    then `target` track changes before the next is even armed."""

    def __init__(self):
        self.prev_music = None     # {"id","title","artist"} of the prior music track
        self.last_music = None     # most recently seen music track
        self.counter = 0
        self.target = None
        self._bounds = None
        self.armed = None          # #5986: {"id", "prev"} of the track whose end fires the bridge
        self.last_skip_reason = None  # informational only, for the run loop's logging

    @staticmethod
    def _clamp(settings):
        every_min = max(1, int(settings.get("every_min", 3)))  # #5986
        every_max = max(every_min, int(settings.get("every_max", 3)))
        return every_min, every_max

    def _maybe_redraw(self, settings):
        bounds = self._clamp(settings)
        if self.target is None or bounds != self._bounds:
            self.target = random.randint(bounds[0], bounds[1])
            self._bounds = bounds

    @staticmethod
    def _position_s(state, now):  # #5986: extrapolate from the device's last post
        position_s = (state.get("position_ms") or 0) / 1000.0
        updated_at = state.get("updated_at")
        if state.get("playing") and isinstance(updated_at, (int, float)) and 0 < now - updated_at < 120:
            position_s += now - updated_at
        return position_s

    @staticmethod
    def _music_items(state, ahead=True):  # #6038 queue entries that are SONGS, nearest first
        queue_index = state.get("queue_index")  # #6038
        if not isinstance(queue_index, int):  # #6038
            return []  # #6038
        items = [q for q in state.get("queue") or []  # #6038
                 if isinstance(q.get("index"), int) and isinstance(q.get("id"), int) and q["id"] > 0  # #6038 DJ clips / streams are not beats
                 and (q["index"] > queue_index if ahead else q["index"] < queue_index)]  # #6038
        return sorted(items, key=lambda q: q["index"], reverse=not ahead)  # #6038

    @classmethod  # #6038
    def _next_item(cls, state, offset=1):  # #6005 offset 2 = the song after next
        # #6038: the offset-th SONG ahead in the real queue. index+offset alone named a DJ
        # clip as 'coming up next' whenever one sat between two songs.
        items = cls._music_items(state)  # #6038
        if 1 <= offset <= len(items):  # #6038
            q = items[offset - 1]  # #6038
            return {"id": q.get("id"), "title": q.get("title"), "artist": q.get("artist")}  # #5986 id -> facts
        return None

    @classmethod  # #6038
    def _prev_item(cls, state):  # #6038 the song before this one in the queue
        items = cls._music_items(state, ahead=False)  # #6038
        return {"id": items[0].get("id"), "title": items[0].get("title"), "artist": items[0].get("artist")} if items else None  # #6038 #3912 id -> callout

    def _reset_after_fire(self, settings):
        self.counter = 0
        self.armed = None
        self._bounds = None
        self.target = None
        self._maybe_redraw(settings)  # redraw target after every fire

    def _payload(self, mode, track, prev, state, now):  # #5986
        prev_payload = {"id": prev.get("id"), "title": prev.get("title"), "artist": prev.get("artist")} if prev else None  # #3912 id -> callout
        if prev_payload is None:  # #6038 watcher just started: the queue still knows what played before
            prev_payload = self._prev_item(state)  # #6038
        detected_dt = datetime.fromtimestamp(now, tz=timezone.utc)
        return {
            "mode": mode,  # #5986 'outro' | 'intro'
            "prev": prev_payload,
            "now": {
                "id": track.get("id"),  # #5986
                "title": track.get("title"),
                "artist": track.get("artist"),
                "position_s": self._position_s(state, now),
                "duration_s": (state.get("duration_ms") or 0) / 1000.0,
            },
            "next": self._next_item(state),
            "after": self._next_item(state, 2),  # #6005 third beat: 'coming up next is Z' (no facts fetch)
            "cadence": self.target,  # #5986 songs until the next bridge
            "detected_at": detected_dt.isoformat().replace("+00:00", "Z"),
            "detected_epoch": now,
            "source": "audiplex",
        }

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
            # Same track: pause/resume/seek. Not a transition - but the armed
            # track's end is where the outro bridge fires (#5986).
            if self.armed and self.armed["id"] == tid and bool(settings.get("on", False)):
                duration_s = (state.get("duration_ms") or 0) / 1000.0
                remaining = duration_s - self._position_s(state, now)
                if duration_s > 0 and remaining <= OUTRO_LEAD_S:
                    if remaining < 5:  # #5986 too late to talk across the fade; the intro fallback takes it
                        self.last_skip_reason = "armed"
                        return None
                    payload = self._payload("outro", track, self.armed["prev"], state, now)
                    self._reset_after_fire(settings)
                    return payload
            self.last_skip_reason = "same_track"
            return None

        # --- transition ---
        self.prev_music = self.last_music
        self.last_music = {"id": tid, "title": track.get("title"), "artist": track.get("artist")}

        if not bool(settings.get("on", False)):
            self.counter = 0
            self.armed = None  # #5986
            self.last_skip_reason = "off"
            return None

        position_s = (state.get("position_ms") or 0) / 1000.0

        if self.armed is not None:
            # #5986: the armed song ended (or was skipped) before its outro
            # fired - bridge now, at this song's start, like pre-#5986.
            if position_s > 25:
                self._reset_after_fire(settings)
                self.last_skip_reason = "stale"
                return None
            payload = self._payload("intro", track, self.prev_music, state, now)
            self._reset_after_fire(settings)
            return payload

        if position_s > 25:
            # Detected too late (e.g. watcher was down) - reset but don't fire.
            self.counter = 0
            self.last_skip_reason = "stale"
            return None

        self.counter += 1
        if self.counter < self.target:
            self.last_skip_reason = "counting"
            return None

        # #5986: arm this song; its outro fires the bridge.
        self.armed = {"id": tid, "prev": self.prev_music}
        self.last_skip_reason = "armed"
        return None


# --------------------------------------------------------------------------
# I/O: HTTP fetch, hook invocation
# --------------------------------------------------------------------------

def fetch_state(client, base_url, token):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    resp = client.get(f"{base_url}/api/playback/state", headers=headers, timeout=10.0)
    resp.raise_for_status()
    return resp.json()


def fetch_facts(client, base_url, token, track_id):  # #5986
    """Library facts for one track: album, year, genre. Best-effort - any
    failure or placeholder value is simply absent (the persona gets fewer
    facts, never an invented one)."""
    if not isinstance(track_id, int) or track_id <= 0:
        return {}
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        t = client.get(f"{base_url}/api/music/tracks/{track_id}", headers=headers, timeout=FACTS_TIMEOUT_S)
        t.raise_for_status()
        album_id = t.json().get("album_id")
        a = client.get(f"{base_url}/api/music/albums/{album_id}", headers=headers, timeout=FACTS_TIMEOUT_S)
        a.raise_for_status()
        album = a.json()
    except Exception as exc:
        log_line(f"facts fetch failed for {track_id}: {exc!r}")
        return {}
    facts = {}
    album_artist = (album.get("artist_name") or "").strip()  # #6038
    if album_artist:  # #6038 Pantheon states an album only when this matches the track's artist:
        facts["album_artist"] = album_artist  # #6038 a folder's album record is 'Various Artists'/'Individual'/'faster'
    title = (album.get("title") or "").strip()
    if title and title.lower() not in ("unknown", "unknown album", "music", "various"):
        facts["album"] = title
    year = album.get("year")
    if isinstance(year, int) and 1900 < year < 2100:
        facts["year"] = year
    genre = (album.get("genre") or "").strip()
    if genre and genre.lower() not in ("unknown", "other", "misc"):
        facts["genre"] = genre
    return facts


def fetch_callouts(client, base_url, token, ids):  # #3912
    """{id: {callout, why}} for the bridge's songs: name the deep cuts, not the
    songs Todd knows. consume=true counts a play off any "what was that?"
    exception. Any failure = {} and the bridge names every song, as before."""
    ids = [i for i in ids if isinstance(i, int) and i > 0]
    if not ids:
        return {}
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        r = client.get(f"{base_url}/api/playback/callouts", headers=headers, timeout=FACTS_TIMEOUT_S,
                       params={"ids": ",".join(map(str, ids)), "consume": "true"})
        r.raise_for_status()
        return {int(k): v for k, v in (r.json().get("tracks") or {}).items()}
    except Exception as exc:
        log_line(f"callouts fetch failed: {exc!r}")
        return {}


def enrich_payload(payload, client, base_url, token):  # #5986
    for key in ("now", "next"):
        item = payload.get(key)
        if item:
            item["facts"] = fetch_facts(client, base_url, token, item.get("id"))
    beats = [payload.get(k) for k in ("prev", "now", "next", "after") if payload.get(k)]  # #3912
    calls = fetch_callouts(client, base_url, token, list(dict.fromkeys(b.get("id") for b in beats)))  # #3912
    for item in beats:  # #3912
        c = calls.get(item.get("id"))
        if c:
            item["callout"], item["callout_why"] = bool(c.get("callout")), c.get("why")
    return payload


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
            reason = counter.last_skip_reason
            if payload is not None or reason in ("counting", "off", "stale", "init") or (
                    reason == "armed" and counter.armed and not getattr(counter, "_armed_logged", False)):  # #5986
                # #2858: one line per track change, so a missing bridge is explainable
                title = (state.get("track") or {}).get("title")
                verdict = f"FIRE {payload['mode']}" if payload is not None else reason  # #5986
                log_line(f"track -> {title!r}: {verdict} ({counter.counter}/{counter.target})")
                counter._armed_logged = reason == "armed"  # #5986 one 'armed' line per song
            if payload is not None:
                fire_hook(enrich_payload(payload, client, base_url, token))  # #5986

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
