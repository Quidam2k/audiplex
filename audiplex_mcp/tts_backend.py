"""Pluggable text-to-speech for DJ voice breaks (item #431).

Primary backend is an **OpenAI-compatible HTTP endpoint** (locked decision:
keep Audiplex decoupled from Pantheon — Todd's standing keep-apps-separate
ruling, #439). Any server speaking `POST /v1/audio/speech` works: OpenAI
itself, a local Piper/Kokoro/openedai-speech shim, or Pantheon's own speech
service sitting behind that URL. Audiplex neither knows nor cares which.

Config:
  DJ_TTS_URL      base URL or full endpoint of an OpenAI-compatible TTS
                  service, e.g. http://localhost:8880 or
                  http://localhost:8880/v1/audio/speech
  DJ_TTS_MODEL    model name (default "tts-1")
  DJ_TTS_VOICE    voice id (default "alloy") — Todd's DJ voice choice slots
                  in here; nothing in this package hardcodes a persona voice
  DJ_TTS_FORMAT   wav | mp3 (default "wav")
  DJ_TTS_API_KEY  optional bearer token

  DJ_TTS_CMD      escape hatch: a shell command template containing {text}
                  and {out} placeholders, e.g.
                  'my-tts --say {text} --out {out}'. Used only when
                  DJ_TTS_URL is unset. Deliberately generic — it is NOT
                  wired to any particular project's synthesizer.

Precedence: DJ_TTS_URL, then DJ_TTS_CMD, else a clear configuration error.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
from pathlib import Path

import httpx

DEFAULT_MODEL = "tts-1"
DEFAULT_VOICE = "alloy"
DEFAULT_FORMAT = "wav"
SYNTH_TIMEOUT_SECONDS = 180.0  # local models can be slow to warm up


class TtsNotConfigured(RuntimeError):
    """Neither backend is configured — surfaced to the agent as guidance."""


class TtsFailed(RuntimeError):
    """A configured backend was reached but did not produce audio."""


def is_configured() -> bool:
    return bool(os.environ.get("DJ_TTS_URL") or os.environ.get("DJ_TTS_CMD"))


def describe() -> str:
    if os.environ.get("DJ_TTS_URL"):
        return (
            f"OpenAI-compatible endpoint {_endpoint()} "
            f"(model={os.environ.get('DJ_TTS_MODEL', DEFAULT_MODEL)}, "
            f"voice={voice()}, format={audio_format()})"
        )
    if os.environ.get("DJ_TTS_CMD"):
        return f"subprocess template: {os.environ['DJ_TTS_CMD']}"
    return "not configured"


def audio_format() -> str:
    fmt = (os.environ.get("DJ_TTS_FORMAT") or DEFAULT_FORMAT).lower().lstrip(".")
    return fmt if fmt in {"wav", "mp3"} else DEFAULT_FORMAT


def voice() -> str:
    return os.environ.get("DJ_TTS_VOICE") or DEFAULT_VOICE


def _endpoint() -> str:
    """Accept either a base URL or a full endpoint."""
    url = (os.environ.get("DJ_TTS_URL") or "").rstrip("/")
    if not url:
        return ""
    if "/audio/speech" in url:
        return url
    if url.endswith("/v1"):
        return f"{url}/audio/speech"
    return f"{url}/v1/audio/speech"


async def _synth_http(text: str, out_path: Path) -> Path:
    payload = {
        "model": os.environ.get("DJ_TTS_MODEL", DEFAULT_MODEL),
        "input": text,
        "voice": voice(),
        "response_format": audio_format(),
    }
    headers = {}
    api_key = os.environ.get("DJ_TTS_API_KEY")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    async with httpx.AsyncClient(timeout=SYNTH_TIMEOUT_SECONDS) as client:
        try:
            resp = await client.post(_endpoint(), json=payload, headers=headers)
        except httpx.HTTPError as e:
            raise TtsFailed(f"TTS request to {_endpoint()} failed: {e}") from e
    if resp.status_code >= 400:
        raise TtsFailed(
            f"TTS endpoint {_endpoint()} returned {resp.status_code}: "
            f"{resp.text[:300]}"
        )
    if not resp.content:
        raise TtsFailed("TTS endpoint returned an empty body")
    out_path.write_bytes(resp.content)
    return out_path


def _synth_cmd(text: str, out_path: Path) -> Path:
    template = os.environ["DJ_TTS_CMD"]
    if "{out}" not in template:
        raise TtsNotConfigured("DJ_TTS_CMD must contain an {out} placeholder")
    # Substitute after splitting so text containing spaces/quotes stays one argv
    # entry instead of being re-parsed as extra arguments.
    argv = [
        part.replace("{text}", text).replace("{out}", str(out_path))
        for part in shlex.split(template, posix=False)
    ]
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=SYNTH_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise TtsFailed(f"DJ_TTS_CMD failed to run: {e}") from e
    if proc.returncode != 0:
        raise TtsFailed(
            f"DJ_TTS_CMD exited {proc.returncode}: "
            f"{(proc.stderr or b'').decode(errors='replace')[:300]}"
        )
    if not out_path.is_file() or out_path.stat().st_size == 0:
        raise TtsFailed(f"DJ_TTS_CMD produced no audio at {out_path}")
    return out_path


async def synthesize(text: str) -> Path:
    """Render `text` to an audio file and return its path.

    The caller owns the returned file and should delete it after upload.
    """
    text = (text or "").strip()
    if not text:
        raise TtsFailed("Nothing to synthesize (empty text)")
    if not is_configured():
        raise TtsNotConfigured(
            "No TTS backend configured. Set DJ_TTS_URL to an OpenAI-compatible "
            "speech endpoint (e.g. http://localhost:8880), optionally with "
            "DJ_TTS_MODEL / DJ_TTS_VOICE / DJ_TTS_FORMAT — or set DJ_TTS_CMD to "
            "a command template containing {text} and {out}."
        )

    fd, name = tempfile.mkstemp(prefix="djbreak-", suffix=f".{audio_format()}")
    os.close(fd)
    out_path = Path(name)
    try:
        if os.environ.get("DJ_TTS_URL"):
            return await _synth_http(text, out_path)
        return _synth_cmd(text, out_path)
    except BaseException:
        out_path.unlink(missing_ok=True)
        raise
