"""Pre-rendered DJ clip bank: dj_announce's fallback when live TTS fails (#3917).

Pantheon renders the bank and writes a manifest; Audiplex only reads it, so
there is no Pantheon import here (keep-apps-separate, #439). Manifest shape:

    {"version": 1,
     "voices": {voice: {clip_id: {"text", "path"}}},
     "artist_ids": {artist: clip_id},
     "<kind>_ids": {key: clip_id}, ...}

Config:
  DJ_TTS_VOICE      the DJ voice. Unset means no fallback, ever: the
                    tts_backend "alloy" default never selects a bank voice.
  DJ_CLIP_MANIFEST  path to dj_manifest.json. Unset means no fallback.

A kind is either a clip id (dj_next, ride_climb, ...) or a keyed family: kind
"that_was" + key "Cat Stevens" resolves through "artist_ids", any other kind K
through "K_ids", and a miss falls back to "K_generic". New families (per-track
title clips, #3919) slot in with a manifest change only.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

VOICE_ALIASES = {"jarvis": "claude", "karen": "gemini"}  # same as Pantheon tts_render_cli
ID_MAPS = {"that_was": "artist_ids"}  # kinds whose key map predates the <kind>_ids rule
DEFAULT_KIND = "dj_next"


def manifest_voice() -> str | None:
    """DJ_TTS_VOICE -> manifest voice key, or None when no DJ voice is configured."""
    name = (os.environ.get("DJ_TTS_VOICE") or "").strip().lower()
    if not name:
        return None
    return VOICE_ALIASES.get(name, name)


def load_manifest() -> dict | None:
    """Read the manifest fresh (it is small). Unset, missing or unreadable -> None."""
    path = os.environ.get("DJ_CLIP_MANIFEST")
    if not path:
        return None
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def lookup(manifest: dict, voice: str, kind: str, key: str | None = None) -> tuple[str, str] | None:
    """(clip_id, path) for this voice and kind, or None. Never approximates copy:
    a kind the manifest lacks, or a file that is not on disk, is a miss."""
    clips = (manifest.get("voices") or {}).get(voice)
    if not isinstance(clips, dict):
        return None
    ids = manifest.get(ID_MAPS.get(kind, f"{kind}_ids"))
    if isinstance(ids, dict) or kind in ID_MAPS:
        cid = (ids or {}).get(key) if key else None
        if cid not in clips:
            cid = f"{kind}_generic"
    else:
        cid = kind
    entry = clips.get(cid)
    if not isinstance(entry, dict) or not entry.get("path"):
        return None
    return (cid, entry["path"]) if Path(entry["path"]).is_file() else None


def fallback_clip(kind: str | None = None, key: str | None = None) -> tuple[str, str] | None:
    """The bank clip for the configured DJ voice, or None (fallback dormant / miss)."""
    voice = manifest_voice()
    if voice is None:
        return None
    manifest = load_manifest()
    if manifest is None:
        return None
    return lookup(manifest, voice, kind or DEFAULT_KIND, key)
