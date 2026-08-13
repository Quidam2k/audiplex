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

## Tools (v1)

- `dj_play_now(track_ids: list[int])` — replace the queue and play these tracks now.
- `dj_now_playing()` — what's playing right now.

Resolve track names → IDs via the catalog API (`GET /api/music/albums`,
`/api/music/artists/{id}/tracks`, etc.). A dedicated `dj_search` and the
incremental queue tools (`dj_queue`, `dj_play_next`, `dj_skip`) arrive in P3.

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
