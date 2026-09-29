"""Browser UI (#2806): a static page at /web that drives the existing API.

The page is public (it is only HTML/JS/CSS); everything it reads or writes goes
through the /api routes, which keep their own auth. It never plays audio: "play"
sends a bus command to the phone or PC renderer.

Security: the page holds the owner's long-lived token in localStorage, so the
CSP forbids inline and external script, and app.js renders untrusted library
metadata with textContent only (a test enforces both).
"""

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, RedirectResponse

router = APIRouter(tags=["web"])

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
_FILES = {
    "": ("index.html", "text/html; charset=utf-8"),
    "index.html": ("index.html", "text/html; charset=utf-8"),
    "app.js": ("app.js", "text/javascript; charset=utf-8"),
    "style.css": ("style.css", "text/css; charset=utf-8"),
}
_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; "
        "frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-cache",
}


@router.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/web/")


@router.get("/web", include_in_schema=False)
def web_bare():
    return RedirectResponse("/web/")


@router.get("/web/{name:path}", include_in_schema=False)
def web_file(name: str):
    # A fixed allowlist, so no path from the URL ever reaches the filesystem.
    entry = _FILES.get(name)
    if entry is None:
        raise HTTPException(status_code=404)
    filename, media_type = entry
    return FileResponse(WEB_DIR / filename, media_type=media_type, headers=_HEADERS)
