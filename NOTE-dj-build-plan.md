# AUDIPLEX DJ — BUILD PLAN (assignment #1782 / PROCEED #1784)

Status: **P1 COMPLETE** (server + MCP runtime-verified; Android written, compile pending). Awaiting direction for P2. cwd: Z:\Development\audiplex

## P1 — what shipped & how to run
Server (verified — full pytest suite 214 passed, incl. 6 new playback tests):
- `server/audiplex/playback_bus.py` — global in-memory queue + now-playing state (single-device v1)
- `server/audiplex/routers/playback.py` — POST /api/playback/command, GET /api/playback/command/next (long-poll ~25s→204), POST/GET /api/playback/state
- `server/audiplex/schemas.py` (+PlaybackCommand/PlaybackCommandQueued/NowPlayingTrack/PlaybackState), `main.py` (router wired)
- `server/audiplex/create_service_token.py` — mints dj-agent JWT: `cd server && python -m audiplex.create_service_token`
- `server/tests/test_api_playback.py`, `conftest.py` (router added to test app)
- Run server: `cd server && uvicorn audiplex.main:app --host 0.0.0.0 --port 8000`

MCP (verified — imports, both tools register as audiplex-dj):
- `audiplex_mcp/{__init__,server}.py`, `requirements.txt`, `README.md` — tools dj_play_now, dj_now_playing
- Run: `cd audiplex_mcp && pip install -r requirements.txt` then from repo root `AUDIPLEX_URL=… AUDIPLEX_TOKEN=… python -m audiplex_mcp.server`

Android (written, NOT compiled here — SDK path mismatch on this machine):
- `data/api/ApiModels.kt` (+DjCommandPayload/DjCommandDto/NowPlayingTrackDto/PlaybackStateDto)
- `data/api/AudiplexApi.kt` (+getNextPlaybackCommand long-poll, +postPlaybackState)
- `playback/DjCommandClient.kt` — @Singleton; commandLoop (long-poll→playTracks) + reportLoop (5s now-playing POST)
- `AudiplexApp.kt` — injects + starts DjCommandClient in onCreate
- Verify: `cd android && ./gradlew assembleDebug` (best on Todd's machine; local.properties sdk.dir → C:\Users\Todd\…\Sdk)

## Env note (test infra)
bcrypt 5.0.0 breaks passlib 1.7.4 (`__about__` removed). Pinned bcrypt==4.0.1 locally to run tests. Consider pinning `passlib[bcrypt]` → bcrypt<4.1 (or migrate off passlib) in pyproject. NOT done — flag for Todd.

## Loop proof (end-to-end, once Android built)
agent→dj_play_now→POST /command → client long-poll picks it up → playTracks() via Media3 → client POSTs /state → agent dj_now_playing reads it. P1 uses ZERO new Media3 queue ops + no always-on service (rides PlaybackManager scope while process alive).

---
## ORIGINAL PLAN (approved)


## Locked decisions (Todd)
Q1 loose unification · Q2 screen-off, no FCM · Q3 music-first v1 · Q4 dedicated MCP server · Q5 single device · Q6 transient queueing only.

## Architecture confirmed by investigation
- Server (FastAPI) is stateless catalog + byte-streamer. JWT Bearer auth via `get_current_user`. No `/search` endpoint exists.
- Android: `PlaybackManager` (@Singleton) holds a **MediaController** connected to `PlaybackService` (MediaSessionService, foregroundServiceType=mediaPlayback). Singleton `CoroutineScope(SupervisorJob+Main)` already runs 250ms position loop + 30s progress sync — perfect host for a command collector + now-playing reporter while the process is alive.
- Existing queue ops: setMediaItems / setMediaItem / seekToNext/Prev / seekTo(index). MISSING: addMediaItem, insertMediaItem (play-next), moveMediaItem (reorder), removeMediaItem.
- Track→MediaItem builders + URL/auth (`AudiplexApi.musicStreamUrl`, AuthInterceptor Bearer) already exist.
- Manifest perms: INTERNET, FOREGROUND_SERVICE, FOREGROUND_SERVICE_MEDIA_PLAYBACK, FOREGROUND_SERVICE_DATA_SYNC, POST_NOTIFICATIONS. NOT declared: WAKE_LOCK, RECEIVE_BOOT_COMPLETED.

## Transport recommendation: LONG-POLL (not WS) for v1
Reuses existing Retrofit/OkHttp (no new lib), trivial reconnect (re-issue the call), robust over flaky Tailscale, DJ command cadence is low-freq so latency is moot. Server = per-user asyncio.Queue, GET blocks ~25s then 204. WS is the documented fast-follow if latency ever matters (OkHttp+FastAPI both support it).

## Cold-start handling: RECOMMEND documented limitation + durable server queue (option A)
- Screen-off DURING playback = covered free (foreground MediaSessionService keeps process+collector alive).
- COLD (process dead, screen off): command is **persisted server-side** and drains on next app/process wake — no command lost, just deferred. v1 limitation documented.
- Option B (fast-follow): dedicated always-on `DjCommandService` (dataSync FGS) + RECEIVE_BOOT_COMPLETED for true cold-start, at the cost of a permanent notification + battery. Defer unless Todd wants silent-cold play.

## Phases
P1 (tight prove-the-loop slice):
- Server NEW `routers/playback.py` + `playback_bus.py` (per-user asyncio.Queue + last-state dict): POST /api/playback/command, GET /api/playback/command/next (long-poll), POST /api/playback/state, GET /api/playback/state. main.py include_router. schemas.py: PlaybackCommand, PlaybackState. Command type for P1: `play_now` (track_ids[] or album_id).
- Android NEW `playback/DjCommandClient.kt` (long-poll loop in PlaybackManager scope → dispatch). PlaybackManager: now-playing reporter (combine currentMusic/isPlaying/positionMs → POST state). AudiplexApi: 4 endpoints + DTOs. Wire collector start.
- MCP NEW dedicated `audiplex_mcp/server.py`: dj_play_now, dj_now_playing (+ resolve via catalog).
- Loop proven with ZERO new Media3 queue ops, no new always-on service.
- DECISION for Jarvis: add tiny `GET /api/music/search?q=` in P1 (clean dj_search) vs resolve client-side in MCP via existing list endpoints.

P2 Android incremental queue ops (addMediaItem/insertMediaItem/moveMediaItem wrap) + command types queue/play_next/skip/reorder + reporter robustness. (Option-B cold-start service belongs here if chosen.)
P3 Full MCP DJ toolset: dj_queue, dj_play_next, dj_skip, dj_search, dj_now_playing + catalog resolution helpers.
P4 (optional) Pantheon now-playing widget reading GET /api/playback/state.

## Open decisions for signoff
1. Approve long-poll over WS for v1? 2. Approve cold-start = documented limitation + durable queue (A) vs build DjCommandService now (B)? 3. Add `/api/music/search` in P1 or resolve in MCP? 4. MCP auth: dedicated service-account JWT user vs reuse Todd's token?
