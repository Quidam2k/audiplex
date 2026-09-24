# 2026-09-24 — Audiplex Windows "follow-me" client (#2021 / assignment #5038)

Worker: worker-audiplex-win-client--20260924-203352-632b. Plan-back submitted (high-crit), awaiting signoff.

## What already exists (codebase map)
- **Command bus already present**: `server/audiplex/playback_bus.py` — global in-memory singleton
  `bus = PlaybackBus()`. Routes in `server/audiplex/routers/playback.py` (`/api/playback/*`):
  `POST command`, `GET command/next` (25s long-poll, 204 on timeout), `POST command/{id}/ack`,
  `POST state`, `GET state`, `GET device`. Schemas in `schemas.py`: `PlaybackState`,
  `NowPlayingQueueItem`, `PlaybackCommand`.
- **Phone = current sole renderer**: `android/.../playback/DjCommandClient.kt` — two loops:
  `commandLoop()` long-polls `command/next` → dispatch → ack; `reportLoop()` POSTs `state` every 5s.
  `DjLinkService.kt` foreground service keeps the process alive.
- **DJ MCP**: `audiplex_mcp/server.py` (FastMCP "audiplex-dj"), 36 `dj_*` tools, drives bus via
  `POST /api/playback/command` as service account `dj-agent`. Music-focused (not audiobooks).
- **Auth**: JWT HS256, `Authorization: Bearer` header ONLY (no `?token=`), host-gated
  (`android/.../data/ApiModule.kt` AuthInterceptor). Service token via
  `server/audiplex/create_service_token.py`; token file `.dj_token` at repo root.
- **No websockets/SSE** anywhere — pure HTTP long-poll + push.
- Server on **:8100** (`launch.bat`, `launch-hidden.vbs`). Serves BOTH music and audiobooks;
  durable audiobook resume is per-book/user in `PlaybackPosition` (`/api/progress`).
- Gotcha: MCP defaults `AUDIPLEX_URL` to `:8000`; real server is `:8100` — env must override.

## Core gap
Bus is global/single-device: commands aren't addressed to a device, one shared queue+state.
Two live renderers double-play. → Need **"active device" targeting** (the real new work).

## Design (proposed)
- **Stack**: Python 3.11 client + **python-vlc** (libVLC handles AAC/m4b/opus/mp3/range/seek/volume/gapless).
- **Stream auth**: tiny in-process **local HTTP proxy** (127.0.0.1) injects Bearer + forwards range to :8100;
  point VLC at loopback. Zero server auth change; JWT never leaks.
- **Server (additive)**: device registry on the bus (device_id/name/type/last_seen) + `active_device_id`;
  `GET /api/playback/devices`, `POST /api/playback/devices/{id}/activate`; `command/next` targets active
  device (back-compat: unchanged when no device registered → phone keeps working). DJ tools `dj_devices`,
  `dj_transfer`.
- **Handoff**: exactly one active renderer. Transfer → phone gets `deactivate` (pause + report position),
  PC resumes queue at `state.position_ms` (music) / `/api/progress` (audiobook).

## Slices
- **Slice 1 (this week)**: Windows Python tray client renders MUSIC from bus via proxy+vlc; server device
  registry + activate + targeting (back-compat); `dj_transfer("Solace")`. No phone auto-pause yet.
  Test on Solace/Athena (NEVER the phone), no focus-stealing windows.
- **Slice 2**: phone auto-pause/resume (Android change) + true bidirectional handoff at exact position
  + audiobook follow-me.

## Codex-first
Draft PC client (bus loops, vlc wrapper, local proxy, tray) via `codex_draft.sh`; review/test/install +
ledger verdict. Server bus edits written directly (tested module) but diff-reviewed via `codex_run.sh --pipe`.
Tests: extend `server/tests/test_api_playback.py`, `test_playback_registry.py`.

## Open questions (in plan-back)
1. Tray/headless vs visible window (rec: headless tray).
2. Reuse `dj-agent` token vs mint per-device `pc-solace` (rec: per-device).
3. Slice 1 = music only, audiobook deferred — OK?
4. Android deactivate/reactivate change in this cascade or separate assignment?
