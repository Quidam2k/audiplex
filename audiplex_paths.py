"""Machine-local roots for paths outside this repo (#5457).

Every path default that points outside the repo is derived from one of these roots, so a host
move (Solace -> Athena) is one edit to local_paths.json (or one env var), not twenty code edits.
No machine path lives in this repo (its remote is public).

Lookup order for each root:
  1. its env var (AUDIPLEX_PANTHEON_ROOT, AUDIPLEX_DEV_ROOT, AUDIPLEX_COMFY_OUTPUT, AUDIPLEX_MUSIC_ROOT)
  2. local_paths.json at the repo root (gitignored; copy local_paths.example.json)
A root set in neither RAISES LocalPathsError naming the file to create and the missing key, and
so does a malformed local_paths.json: a box never quietly guesses another machine's paths.
A per-site env var (DJ_SPEECH_STATE_FILE, DJ_PANTHEON_DB, ...) still beats all of this.
Resolved on every call (the server/scripts CLIs once at start). Stdlib only.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
LOCAL_FILE = REPO_ROOT / "local_paths.json"

# key in local_paths.json -> env var that overrides it
ROOTS = {
    "pantheon_root": "AUDIPLEX_PANTHEON_ROOT",
    "dev_root": "AUDIPLEX_DEV_ROOT",
    "comfy_output": "AUDIPLEX_COMFY_OUTPUT",
    "music_root": "AUDIPLEX_MUSIC_ROOT",
    "tts_remote_url": "PANTHEON_TTS_REMOTE_URL",  # #5648 Athena's render URL (a LAN address, so never in the repo)
    "dj_tts_voice": "DJ_TTS_VOICE",  # #5648 the server's DJ voice; the supervisor launches it with no env
}


class LocalPathsError(RuntimeError):
    """A root is not configured, or local_paths.json is not a valid {root: "path"} object."""


def _local() -> dict[str, str]:
    path = LOCAL_FILE
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LocalPathsError(f"{path}: unreadable or not JSON ({exc})") from None
    if not isinstance(data, dict):
        raise LocalPathsError(f"{path}: expected a JSON object of root -> path")
    unknown = sorted(set(data) - set(ROOTS))
    if unknown:
        raise LocalPathsError(f"{path}: unknown keys {unknown}; known: {sorted(ROOTS)}")
    for key, value in data.items():
        if not isinstance(value, str) or not value.strip():
            raise LocalPathsError(f"{path}: {key} must be a non-empty path string")
    return {k: v.strip() for k, v in data.items()}


def root(key: str) -> str:
    """The configured value of one root (env > local_paths.json), as a string; unset raises."""
    env_var = ROOTS[key]
    from_env = os.environ.get(env_var, "").strip()
    if from_env:
        return from_env
    value = _local().get(key)
    if value:
        return value
    raise LocalPathsError(
        f"audiplex path root {key!r} is not set. Create {LOCAL_FILE} (copy "
        f"{LOCAL_FILE.with_name('local_paths.example.json').name}) with \"{key}\": \"<path>\", "
        f"or set the env var {env_var}."
    )


def pantheon_root() -> Path:
    return Path(root("pantheon_root"))


def comfy_output() -> Path:
    return Path(root("comfy_output"))


def _slashes(value: str) -> str:
    """Forward slashes, no trailing one: these strings also go into shlex-split commands."""
    return value.replace("\\", "/").rstrip("/")


def pantheon(*parts: str) -> str:
    """A path under the Pantheon root, forward slashes, e.g. pantheon("data", "pantheon.db")."""
    return "/".join([_slashes(root("pantheon_root")), *parts])


def dev(*parts: str) -> str:
    """A path under the Development root, forward slashes."""
    return "/".join([_slashes(root("dev_root")), *parts])


def expand(text: str) -> str:
    """Expand {pantheon_root} / {dev_root} / {comfy_output} / {music_root} in a config string."""
    out = text
    for key in ROOTS:
        token = "{" + key + "}"
        if token in out:
            out = out.replace(token, root(key).rstrip("/\\"))
    return out
