"""#2806 isolated end-to-end: the browser UI in headless Chromium at phone size.

    C:\\Python311\\python.exe server/tests/web_e2e_2806.py [--port 8199]

Stands up a throwaway server (temp config.yaml + fresh temp DB) on 127.0.0.1:<port>
with three short silent tracks, one titled with an HTML injection payload. A
scripted fake renderer "e2e-test-device" is the ONLY device on the bus; it plays
nothing and just records the commands it gets. Headless Chromium (no window)
signs in, browses, favorites, rates, builds and reorders a playlist, sends a play
to the e2e device, reloads, and signs out; each step is checked through the API.
Refuses to run on :8100, on the live DB, on a busy port, or if any device id could
be the real phone. Exit code 0 = every check passed.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright

SERVER = Path(__file__).resolve().parents[1]
LIVE_PORT = 8100
LIVE_DB = (SERVER / "audiplex.db").resolve()
TEST_ID = "e2e-test-device"
XSS_TITLE = '<img src=x onerror="window.__xss=1">Evil'
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


def _mint(tmp: Path, env: dict, username: str) -> str:
    out = subprocess.run([sys.executable, "-m", "audiplex.create_service_token", "--username", username],
                         cwd=tmp, env=env, capture_output=True, text=True, check=True).stdout
    for line in out.splitlines():
        if "AUDIPLEX_TOKEN=" in line:
            return line.split("AUDIPLEX_TOKEN=", 1)[1].strip()
    raise RuntimeError(f"no token in: {out}")


class FakeRenderer(threading.Thread):
    """Polls the bus as the e2e device and records (never plays) what it gets."""

    def __init__(self, base: str, token: str) -> None:
        super().__init__(daemon=True)
        self.client = httpx.Client(base_url=base, headers={"Authorization": f"Bearer {token}"}, timeout=35)
        self.received: list[dict] = []
        self.stop = threading.Event()

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
            self.client.post(f"/api/playback/command/{cmd['id']}/ack", json={"status": "ok"})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8199)
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="audiplex-e2e-2806-"))
    db_path = tmp / "audiplex.db"
    _guard(args.port, db_path)
    base = f"http://127.0.0.1:{args.port}"

    album = tmp / "lib" / "music" / "E2E Artist" / "E2E Album"
    album.mkdir(parents=True)
    for n, title in ((1, "Alpha Song"), (2, "Beta Song"), (3, XSS_TITLE)):
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
        "jwt_secret": "e2e-2806-" + "0" * 40,
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
        # First account on an empty DB becomes the admin (the owner, like Todd).
        owner = httpx.post(f"{base}/api/auth/register",
                           json={"username": "owner", "password": "e2e-pass"}).json()["token"]
        api = httpx.Client(base_url=base, headers={"Authorization": f"Bearer {owner}"}, timeout=30)
        api.post("/api/library/scan")
        tracks = _wait(lambda: len(t := _tracks(api)) >= 3 and t, 60, "tracks scanned")
        by_title = {t["title"]: t["id"] for t in tracks}
        note(f"server :{args.port} up in {tmp}; tracks {by_title}")

        fake = FakeRenderer(base, _mint(tmp, env, TEST_ID))
        fake.start()
        _wait(lambda: any(d["id"] == TEST_ID for d in api.get("/api/playback/devices").json()["devices"]),
              30, "e2e device registered")
        ids = {d["id"] for d in api.get("/api/playback/devices").json()["devices"]}
        if not check(ids == {TEST_ID}, f"only e2e devices on the bus: {sorted(ids)}"):
            return 1

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
            console_errors: list[str] = []
            page.on("console", lambda m: m.type == "error" and console_errors.append(m.text))
            page.on("pageerror", lambda e: console_errors.append(str(e)))
            page.on("dialog", lambda d: d.accept())

            page.goto(f"{base}/")
            check(page.url == f"{base}/web/", f"/ redirects to the page ({page.url})")
            page.fill("#username", "owner")
            page.fill("#password", "wrong")
            page.click("#login-form button[type=submit]")
            page.wait_for_selector("#login-error:text('Wrong username or password.')")
            check(True, "a wrong password is refused on the page")
            console_errors.clear()  # the browser logs that deliberate 401
            page.fill("#password", "e2e-pass")
            page.click("#login-form button[type=submit]")
            page.wait_for_selector("text=E2E Artist")
            check(page.evaluate("Boolean(localStorage.getItem('audiplex.token'))"), "signed in; token stored")

            page.click("text=E2E Artist")
            page.click(".row-link:has-text('E2E Album')")
            page.wait_for_selector(".track")
            check(page.locator(".track .title", has_text="Evil").inner_text() == XSS_TITLE,
                  "an HTML-looking title renders as plain text")
            check(page.evaluate("window.__xss === undefined") and page.locator("#content img").count() == 0,
                  "the injected markup did not run or create elements")

            alpha = page.locator(f".track[data-track-id='{by_title['Alpha Song']}']")
            beta = page.locator(f".track[data-track-id='{by_title['Beta Song']}']")
            alpha.locator("button.fav").click()
            _wait(lambda: any(f["entity_key"] == str(by_title["Alpha Song"])
                              for f in api.get("/api/music/favorites").json()), 10, "favorite saved")
            check(True, "favorite a song → saved on the server")
            beta.locator("select.rating").select_option("4")
            _wait(lambda: any(r["track_id"] == by_title["Beta Song"] and r["rating"] == 4
                              for r in api.get("/api/music/ratings").json()), 10, "rating saved")
            check(True, "rate a song 4 stars → saved on the server")

            alpha.locator("button[aria-label='Add to playlist']").click()
            page.fill("#pick-new", "Ride Mix")
            page.click("#pick-create")
            page.wait_for_selector("#toast:text('Created Ride Mix')")
            beta.locator("button[aria-label='Add to playlist']").click()
            page.click(".pick:has-text('Ride Mix')")
            page.wait_for_selector("#toast:text('Added to Ride Mix')")
            pl = api.get("/api/music/playlists").json()
            detail = api.get(f"/api/music/playlists/{pl[0]['id']}").json() if pl else {}
            check([t["title"] for t in detail.get("tracks", [])] == ["Alpha Song", "Beta Song"],
                  f"playlist built from the page: {[t['title'] for t in detail.get('tracks', [])]}")

            options = page.locator("#device option").all_inner_texts()
            check(not any(o.strip().lower().startswith("phone") for o in options),
                  f"device picker offers only e2e devices: {options}")
            page.select_option("#device", TEST_ID)
            _wait(lambda: api.get("/api/playback/devices").json()["active_device_id"] == TEST_ID, 10, "transfer")
            check(True, "device picker transfers playback to the e2e device")
            alpha.locator("button[aria-label='Play']").click()
            got = _wait(lambda: [c for c in fake.received if c.get("type") == "play_now"], 30, "play delivered")
            check(got[-1]["payload"]["track_ids"] == [by_title["Alpha Song"]],
                  f"Play sent play_now {got[-1]['payload']['track_ids']} to {TEST_ID} (the only device)")

            page.reload()
            page.wait_for_selector("text=E2E Artist")
            check(True, "still signed in after a reload")
            page.click(".tab[data-tab=playlists]")
            page.click(".row-link:has-text('Ride Mix')")
            page.wait_for_selector(".track")
            page.locator(".track").first.locator("button[aria-label='Move down']").click()
            order = lambda: [t["title"] for t in api.get(f"/api/music/playlists/{pl[0]['id']}").json()["tracks"]]
            try:
                _wait(lambda: order() == ["Beta Song", "Alpha Song"], 10, "reorder")
            except TimeoutError:
                pass
            check(order() == ["Beta Song", "Alpha Song"], f"reorder saved: {order()}")
            page.wait_for_selector(".track:first-child:has-text('Beta Song')")
            page.locator(".track").first.locator("button[aria-label='Remove from playlist']").click()
            _wait(lambda: len(api.get(f"/api/music/playlists/{pl[0]['id']}").json()["tracks"]) == 1, 10, "remove")
            check(True, "remove from playlist saved")

            page.click(".tab[data-tab=favorites]")
            page.wait_for_selector(".track:has-text('Alpha Song')")
            check(True, "favorites tab lists the favorited song")
            check(page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"),
                  "no sideways scrolling at phone width (390 px)")
            shot = tmp / "web-2806-phone.png"
            page.screenshot(path=str(shot), full_page=True)
            note(f"screenshot: {shot}")

            page.click("#signout")
            page.wait_for_selector("#login-view:not([hidden])")
            check(page.evaluate("localStorage.getItem('audiplex.token') === null"), "sign out clears the stored token")
            check(not console_errors, f"no console errors or CSP violations: {console_errors[:3]}")
            browser.close()
    finally:
        if fake:
            fake.stop.set()
        server.terminate()
        server.wait(10)
        log.close()

    note(f"RESULT: {'PASS' if not failures else 'FAIL'} ({len(failures)} failed)")
    return 0 if not failures else 1


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


if __name__ == "__main__":
    sys.exit(main())
