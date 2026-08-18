# audiplex_mcp — Audiplex DJ MCP server

A standalone [MCP](https://modelcontextprotocol.io) server that lets an agent
act as a DJ for an Audiplex device: queue music and read now-playing state.
It talks only to the Audiplex REST API, so it stays independent of any
particular agent host (Pantheon or otherwise).

## How it works

```
agent → (MCP tool) → audiplex_mcp → POST /api/playback/command → Audiplex server
                                                                        │
Android client  ── long-poll GET /api/playback/command/next ───────────┘
                └─ executes via Media3, POSTs /api/playback/state ──→ server
agent ← dj_now_playing ← GET /api/playback/state ←──────────────────────┘
```

v1 is **single-device** and **music-first**. Commands wait in a server-side
queue until the client polls, so a command issued while the app is asleep
runs on its next wake (cold-start Option A).

## Tools

Playback — `dj_play_now`, `dj_queue`, `dj_play_next`, `dj_skip`, `dj_previous`,
`dj_pause`, `dj_resume`, `dj_seek`, `dj_volume`, `dj_reorder`, `dj_now_playing`,
`dj_play_stream`, `dj_queue_by`.

Voice — `dj_announce`, `dj_break_brief`.

Browsing (#2943) — `dj_library`, `dj_tracks`, `dj_search`. Note the live library
is a flat untagged dump, so the artist/album/genre axes are degenerate and the
real metadata is in the track titles; `dj_library` says so rather than letting
the agent conclude the library is empty.

Listening signal (#947/#948) — `dj_track_stats` reads completion RATE and where
skips land (sharper than raw play counts: "played 8, finished 2" and "played 2,
finished 2" have the same count and opposite meanings). `dj_check_picks` asks
whether a proposed set would repeat something Todd just heard, at recording
level (this exact track) or work level (any version of the song, 20 min each by
default). Both are advisory — `dj_play_now` / `dj_queue` / `dj_play_next` append
a heads-up when a pick just played but never filter, because an explicit request
for a song is an explicit request.

Taste loop (#2945 phase A) — `dj_recommend` logs a track the library doesn't
have, `dj_rate` records how it landed, `dj_taste` reads the signal back.
Feedback is voice-relayed through the agent; there is no app UI for it.
Stored MCP-side in `data/dj/taste.db` (`DJ_TASTE_DB`), gitignored.

Ingest (#2945 phase B) — `dj_find_candidates` searches for a real source and
downloads **nothing**; `dj_ingest` takes a candidate id from that search plus
Todd's actual words of approval, then downloads, tags, files it under
`<Artist>/<Album>/` (or `Artists & Albums/<Genre>/<Artist>/<Album>/`) and
rescans. The folder layout is not cosmetic — the scanner is path-first, so it
is what makes the track browsable by artist. Needs `ffmpeg` on PATH.

## Setup

1. Mint a service-account token on the server:
   ```bash
   cd server
   python -m audiplex.create_service_token
   # prints AUDIPLEX_TOKEN=...
   ```
2. Install deps:
   ```bash
   cd audiplex_mcp
   pip install -r requirements.txt
   ```
3. Run, with the server URL + token in the environment:
   ```bash
   AUDIPLEX_URL=http://<host>:8000 AUDIPLEX_TOKEN=<token> python -m audiplex_mcp.server
   ```

### Registering with an MCP client

```json
{
  "mcpServers": {
    "audiplex-dj": {
      "command": "python",
      "args": ["-m", "audiplex_mcp.server"],
      "env": {
        "AUDIPLEX_URL": "http://<host>:8000",
        "AUDIPLEX_TOKEN": "<service-account token>"
      }
    }
  }
}
```

Run from the repo root (`Q:\Development\audiplex`) so `audiplex_mcp` is importable.

## DJ voice breaks (item #431)

Two extra tools let the agent talk between songs instead of only pushing
buttons:

- **`dj_break_brief()`** — read-only. Returns the current daypart's persona
  directive, local time, optional weather, and the now-playing snapshot.
- **`dj_announce(text, mode='next', title='DJ break')`** — synthesizes the
  agent's copy to audio, uploads it to `POST /api/dj/clips`, and queues an
  `announce` command. `mode='next'` plays the break after the current song
  (how a real break lands); `mode='now'` interrupts.

The agent writes the copy — nothing here generates prose. See
[`DJ_PERSONA.md`](DJ_PERSONA.md) for the on-air protocol and writing rules.

Speech uses any **OpenAI-compatible** `POST /v1/audio/speech` endpoint, which
keeps Audiplex decoupled from any particular TTS project:

| Env var | Default | Purpose |
|---|---|---|
| `DJ_TTS_URL` | — | base URL or full endpoint (required for `dj_announce`) |
| `DJ_TTS_MODEL` | `tts-1` | model name |
| `DJ_TTS_VOICE` | `alloy` | voice id |
| `DJ_TTS_FORMAT` | `wav` | `wav` or `mp3` |
| `DJ_TTS_API_KEY` | — | optional bearer token |
| `DJ_TTS_CMD` | — | generic subprocess fallback with `{text}`/`{out}` |
| `DJ_PERSONA_NAME` | `the DJ` | on-air name |
| `DJ_LAT` / `DJ_LON` | — | optional keyless Open-Meteo weather line |

Only `dj_announce` needs a TTS backend; the other 13 tools work without one,
and `dj_break_brief` warns when it's unconfigured.

## Verifying the whole lane

```bash
cd server && C:\Python311\python.exe tests/dj_e2e_harness.py
```

Spins up a real server on a throwaway port plus a fake OpenAI-compatible TTS
and drives all 15 tools through the real command bus with a simulated device.
Never touches the production instance on :8100.
