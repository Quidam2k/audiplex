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

Run from the repo root (`Z:\Development\audiplex`) so `audiplex_mcp` is importable.
