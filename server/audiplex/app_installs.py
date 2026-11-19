"""Which Audiplex Android build each phone runs (#4456).

The app already sends app_version_name/code on every state post (#3505); this keeps
the latest per device on disk so Pantheon's get_companion_version_status can see it.
Fail-soft: a disk error never blocks a state post.
"""

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

INSTALLS_PATH = Path(
    os.environ.get("AUDIPLEX_APP_INSTALLS")
    or Path(__file__).resolve().parent.parent / "data" / "app_installs.json"
)
REFRESH_SECONDS = 600.0

_log = logging.getLogger(__name__)
_lock = threading.Lock()
_cache: dict[str, dict[str, Any]] = {}
_loaded = False
# last_seen_at value most recently written to disk, per device
_written_at: dict[str, float] = {}


def _load_locked() -> None:
    """Load once. A read error other than a missing file leaves _loaded False, so the
    next call retries and nothing overwrites a file we could not read."""
    global _loaded
    if _loaded:
        return
    _cache.clear()
    _written_at.clear()
    try:
        raw = json.loads(INSTALLS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raw = {}
    except Exception as exc:
        _log.warning("app installs load failed: %s", exc)
        return
    _loaded = True
    for key, row in (raw.items() if isinstance(raw, dict) else ()):
        try:  # one bad row never costs the other phones' history
            code = row["version_code"]
            if isinstance(code, bool) or not isinstance(code, int):
                continue
            device_id = str(row.get("device_id") or key)
            _cache[device_id] = {
                "device_id": device_id,
                "device_name": row.get("device_name"),
                "version_name": row.get("version_name"),
                "version_code": code,
                "first_seen_at": float(row["first_seen_at"]),
                "last_seen_at": float(row["last_seen_at"]),
            }
            _written_at[device_id] = _cache[device_id]["last_seen_at"]
        except Exception:
            continue


def _write_locked() -> bool:
    try:
        path = Path(INSTALLS_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(_cache, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except Exception as exc:
        _log.warning("app installs write failed: %s", exc)
        return False


def record(
    device_id: str,
    device_name: str | None,
    version_name: str | None,
    version_code: int | None,
    now: float | None = None,
) -> None:
    if isinstance(version_code, bool) or not isinstance(version_code, int):
        return
    try:
        ts = time.time() if now is None else now
        with _lock:
            _load_locked()
            if not _loaded:
                return  # unreadable file: don't overwrite it with one device
            row = _cache.get(device_id)
            must_write = False
            if row is None:
                row = {
                    "device_id": device_id,
                    "device_name": device_name,
                    "version_name": version_name,
                    "version_code": version_code,
                    "first_seen_at": ts,
                    "last_seen_at": ts,
                }
                _cache[device_id] = row
                must_write = True
            else:
                if (
                    row.get("version_code") != version_code
                    or row.get("version_name") != version_name
                ):
                    row["version_code"] = version_code
                    row["version_name"] = version_name
                    row["first_seen_at"] = ts
                    must_write = True
                if device_name != row.get("device_name"):
                    row["device_name"] = device_name
                row["last_seen_at"] = ts
                last_written = _written_at.get(device_id)
                if last_written is None or ts - last_written >= REFRESH_SECONDS:
                    must_write = True
            if must_write:
                _write_locked()  # a failed write also waits REFRESH_SECONDS before retrying
                for did, r in _cache.items():
                    _written_at[did] = r["last_seen_at"]
    except Exception as exc:
        _log.warning("app installs record failed: %s", exc)


def installed(now: float | None = None) -> list[dict]:
    ts = time.time() if now is None else now
    with _lock:
        _load_locked()
        rows = [dict(r) for r in _cache.values()]
    for r in rows:
        r["last_seen_age_seconds"] = round(ts - r["last_seen_at"], 1)
    rows.sort(key=lambda r: r["last_seen_at"], reverse=True)
    return rows


def _reset_for_tests() -> None:
    global _loaded
    with _lock:
        _cache.clear()
        _written_at.clear()
        _loaded = False
