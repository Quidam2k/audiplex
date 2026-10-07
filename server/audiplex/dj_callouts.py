"""Decide whether the DJ should name a song aloud (#3912): deep cuts from whole
albums yes, the songs Todd already knows no, plus "what was that?" exceptions."""

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

EXCEPTION_PLAYS = 3
MODES = ("callout", "silent")

_HIT_DIRECTORY = re.compile(
    r"billboard|countdown|\btop\s*\d+|compilation|greatest hits|\bhits\b",
    re.IGNORECASE,
)


def _state_path() -> Path:
    override = os.environ.get("AUDIPLEX_DJ_CALLOUTS_STATE")
    if override is not None:
        return Path(override)
    return Path(__file__).resolve().parent.parent / "data" / "dj_callouts.json"


def load_state() -> dict:  # #3912
    try:
        with _state_path().open("r", encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, ValueError, UnicodeError):
        return {"sources": {}, "exceptions": []}

    if not isinstance(state, dict):
        return {"sources": {}, "exceptions": []}

    state.setdefault("sources", {})
    state.setdefault("exceptions", [])
    return state


def save_state(state: dict) -> None:  # #3912
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = Path(str(path) + ".tmp")
    with temporary_path.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temporary_path, path)


def norm_artist(name: str | None) -> str:  # #3912
    normalized = " ".join((name or "").lower().split())
    if normalized.startswith("the "):
        normalized = normalized[4:]
    return normalized


def default_for_path(path: str | None) -> tuple[bool, str]:  # #3912
    if not path:
        return False, "no file path"

    components = [part for part in re.split(r"[\\/]+", path) if part]
    directories = components[:-1]
    if directories and re.fullmatch(r"[a-z]:", directories[0], re.IGNORECASE):
        directories = directories[1:]

    for index, component in enumerate(directories):
        if component.lower() != "artists & albums":
            continue

        later_directories = directories[index + 1:]
        for later in later_directories:
            if later.lower() == "mixed" or _HIT_DIRECTORY.search(later):
                return False, f"hit compilation: {later}"

        if len(later_directories) >= 2:
            return True, f"whole album: {later_directories[-1]}"
        return False, "loose files in Artists & Albums"

    location = "\\".join(directories[-2:])
    return False, f"Todd's own picks: {location}"


def _matches(entry: dict, track: dict) -> bool:
    if entry.get("remaining", 0) <= 0:
        return False

    scope = entry.get("scope")
    key = entry.get("key")
    if scope == "track":
        return key == str(track["id"])
    if scope == "artist":
        return bool(key) and key == norm_artist(track.get("artist"))
    return False


def decide(track: dict, lane: str | None, state: dict) -> dict:  # #3912
    for entry in state.get("exceptions", []):
        if _matches(entry, track):
            why = (
                "you asked what this was"
                if entry["scope"] == "track"
                else "you asked about this artist"
            )
            return {
                "callout": True,
                "why": why,
                "exception": True,
                "lane": lane,
            }

    sources = state.get("sources", {})
    if lane in sources:
        mode = sources[lane]
        return {
            "callout": mode == "callout",
            "why": f"source {lane!r} set to {mode}",
            "lane": lane,
        }

    callout, why = default_for_path(track.get("path"))
    return {"callout": callout, "why": why, "lane": lane}


def consume(state: dict, track: dict) -> bool:  # #3912
    changed = False
    retained = []
    for entry in state.get("exceptions", []):
        if _matches(entry, track):
            entry["remaining"] -= 1
            changed = True
            if entry["remaining"] <= 0:
                continue
        retained.append(entry)

    if changed:
        state["exceptions"] = retained
    return changed


def add_exception(state: dict, scope: str, key: str | int, label: str, plays: int = EXCEPTION_PLAYS) -> dict:  # #3912
    if scope not in ("track", "artist"):
        raise ValueError("scope must be 'track' or 'artist'")

    normalized_key = str(key) if scope == "track" else norm_artist(key)
    if not normalized_key:
        raise ValueError("exception key must not be empty")

    entry = {
        "scope": scope,
        "key": normalized_key,
        "label": label,
        "remaining": plays,
        "added_at": datetime.now(timezone.utc).isoformat(),
    }

    exceptions = state.setdefault("exceptions", [])
    replaced = False
    updated = []
    for existing in exceptions:
        if existing.get("scope") == scope and existing.get("key") == normalized_key:
            if not replaced:
                updated.append(entry)
                replaced = True
        else:
            updated.append(existing)

    if not replaced:
        updated.append(entry)
    state["exceptions"] = updated
    return entry


def set_source(state: dict, lane: str, mode: str) -> str | None:  # #3912
    if mode not in ("auto", *MODES):
        raise ValueError("mode must be one of: auto, callout, silent")

    sources = state.setdefault("sources", {})
    if mode == "auto":
        sources.pop(lane, None)
        return None

    sources[lane] = mode
    return mode
