"""#2806: the browser UI at /web is static and public; every write it drives stays behind auth."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from audiplex.routers import music, playback, web


@pytest.fixture
def anon():
    """The real routers with NO auth override: a browser that hasn't signed in."""
    app = FastAPI()
    app.include_router(web.router)
    app.include_router(music.router)
    app.include_router(playback.router)
    return TestClient(app)


def test_web_page_served_without_auth(anon):
    r = anon.get("/web/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "<title>Audiplex</title>" in r.text


def test_web_assets_served(anon):
    r = anon.get("/web/app.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]
    assert anon.get("/web/style.css").status_code == 200


def test_root_redirects_to_web(anon):
    r = anon.get("/", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert r.headers["location"] == "/web/"
    assert anon.get("/web", follow_redirects=False).headers["location"] == "/web/"


def test_web_has_tight_csp(anon):
    csp = anon.get("/web/").headers["content-security-policy"]
    assert "default-src 'self'" in csp
    assert "script-src 'self'" in csp
    assert "unsafe-inline" not in csp.split("style-src")[0]  # no inline scripts
    assert "frame-ancestors 'none'" in csp
    assert anon.get("/web/app.js").headers["x-content-type-options"] == "nosniff"


def test_unknown_or_traversal_paths_404(anon):
    assert anon.get("/web/nope.js").status_code == 404
    assert anon.get("/web/..%2F..%2Fconfig.yaml").status_code == 404
    assert anon.get("/web/%2e%2e/web.py").status_code == 404


def test_page_uses_no_external_or_inline_script(anon):
    html = anon.get("/web/").text
    assert "http://" not in html and "https://" not in html
    assert '<script src="app.js"' in html
    assert html.count("<script") == 1
    js = anon.get("/web/app.js").text
    assert "innerHTML" not in js and "outerHTML" not in js and "insertAdjacentHTML" not in js
    assert "http://" not in js and "https://" not in js


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("post", "/api/music/playlists", {"name": "x"}),
        ("put", "/api/music/playlists/1", {"name": "x"}),
        ("delete", "/api/music/playlists/1", None),
        ("post", "/api/music/playlists/1/tracks", {"track_ids": [1]}),
        ("post", "/api/music/favorites", {"entity_type": "track", "entity_key": "1"}),
        ("delete", "/api/music/favorites/track/1", None),
        ("put", "/api/music/tracks/1/rating", {"rating": 5}),
        ("post", "/api/playback/command", {"type": "play_now", "payload": {"track_ids": [1]}}),
        ("post", "/api/playback/devices/phone/activate", None),
    ],
)
def test_writes_the_page_drives_require_auth(anon, method, path, body):
    kwargs = {"json": body} if body is not None else {}
    assert getattr(anon, method)(path, **kwargs).status_code == 401
