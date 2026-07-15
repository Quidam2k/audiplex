# AUDIPLEX DJ — BUILD PLAN (assignment #1782 / PROCEED #1784)

Status: **P1-P3 + P2c ALL SHIPPED** (item #429, plan-429.json, 2026-07-15). Remaining: Todd's physical on-device smoke test (the true closing gate), production server restart on Solace, and Karen's MCP mount (blocked — see P2c section below). cwd: Q:\Development\audiplex

## P2c done (2026-07-15) — transport commands (pause/resume/previous/seek/volume) + playlist/favorites selection (item #429 last mile)
- **Server:** `PlaybackState.volume`; `Settings.dj_owner_username` (config.yaml, gitignored — set to "admin" locally); owner-resolved read-only `GET /api/playback/playlists`, `/playlists/{id}`, `/favorites` so the dj-agent service account can see Todd's library (his per-user `/api/music/` endpoints return empty for a different caller). pytest 214→219 passed.
- **MCP:** `dj_pause`, `dj_resume`, `dj_previous`, `dj_seek(position_seconds)`, `dj_volume(level 0-100)` (Media3 player volume, NOT device volume — deliberate, see PlaybackManager risk in plan-429.json). `dj_queue_by` gains `kind='playlist'` and `kind='favorites'`, resolved via the new owner-scoped endpoints. `dj_now_playing` reports volume. 12 tools total register.
- **Android:** `DjCommandClient` dispatches the five new command types onto existing `PlaybackManager` entry points (`pause()`, `resume()`, `skipBack()`, `seekTo()`); new `setPlayerVolume`/`playerVolume` (Media3 `controller.volume`). `reportLoop` publishes volume. BUILD SUCCESSFUL, versionCode 27→28.
- **Smoke-tested green** (scratch in-process server + simulated Android client, see workflow in prior smoke-test sections): all 5 transport commands delivered with correct payloads, `dj_now_playing` volume line confirmed, playlist-by-name resolution confirmed end-to-end.
- Commits: server+MCP=ae1fb93, Android=b4e8fc0. (A pre-existing, unrelated, already-complete local-first playback-position feature that was sitting uncommitted got its own commit first, f8ce3a8, to avoid entangling it with #429.)
- **NOT done — blocked:** E3 (mount `audiplex-dj` in Karen's `.mcp.json`) was rejected by the Claude Code auto-mode permission classifier as "unauthorized persistence" (granting a new persona standing network access to issue DJ commands needs explicit sign-off, not just an assignment task). Someone with permission needs to add the block manually or explicitly approve it — see the exact JSON block in plan-429.json step E3 / jarvis/.mcp.json's existing audiplex-dj entry.
- **NOT done — out of reach from Athena:** E2 (restart production server on :8100) — production runs on Solace (192.168.50.139), a separate physical machine from this worker (Athena, 192.168.50.217). No remote exec available; someone on Solace needs to re-run launch.bat (it self-kills the old PID).
- Gemini-Karen's `.gemini/settings.json` has an empty `mcpServers` — flagged to Jarvis, not touched (per plan explicit instruction not to guess at Gemini-side config).

## STEP 1 done (2026-07-03, assignment #1970)
- **Android compiles for the first time.** `./gradlew assembleDebug` → BUILD SUCCESSFUL (1m52s). APK: `android/app/build/outputs/apk/debug/app-debug.apk` (23.6 MB, versionCode 20). DjCommandClient.kt + P1 wiring all compiled clean.
- **SDK path was NOT the blocker** — `local.properties sdk.dir=C:\Android\sdk` already exists (android-34, build-tools 34.0.0, JDK 21). The real blockers on this box (Athena):
  1. Unix `./gradlew` under Git Bash mangles JVM opts → `Could not find or load main class "-Xmx64m"`. Use `gradlew.bat`.
  2. `NoDefaultCurrentDirectoryInExePath` is set → cmd won't run a batch from cwd by bare name. Must prefix `.\`.
  - **Working invocation:** `cmd //c "cd /d Q:\Development\audiplex\android & call .\gradlew.bat assembleDebug --console=plain"`
- **bcrypt pin already committed** (commit 7c9a63e) — `passlib[bcrypt]` + `bcrypt<4.1` in `server/pyproject.toml`. Not "local only" as the old note said. Full `pytest` = **214 passed** (incl. 6 playback tests) with bcrypt 4.0.1.
- **dj_play_now loop smoke-tested GREEN** (server + MCP + simulated Android): ran real `dj_play_now()`/`dj_now_playing()` MCP funcs against a live server on :8011 while a coroutine mimicked DjCommandClient (long-poll → report state). MCP enqueue → long-poll pickup → /state POST → dj_now_playing read-back all verified. Only unexercised leg = actual Media3 playback on Todd's phone (needs the device — Todd's on-phone smoke test).
- Side effects: minted `dj-agent` service account (id=2) in server/audiplex.db; versionCode auto-bumped for each build.

## P2a done (2026-07-03) — dj_skip, zero new Media3 ops
- MCP: new `dj_skip()` tool → POST /command {type:"skip"}. Verified registered (dj_play_now, dj_now_playing, dj_skip) + smoke-tested green (MCP → server → client picks up type=skip).
- Android: DjCommandClient dispatch now handles "skip" → `playbackManager.skipForward()` (music → existing `seekToNextMediaItem`, zero new queue ops). Rebuilt: BUILD SUCCESSFUL.
- Server/bus: NO changes (PlaybackCommand.type is free-form str, payload defaults {}) — "no bus/architecture changes" held.
- Changes are in the WORKING TREE, not committed (awaiting Todd/Jarvis on commit cadence). Files: audiplex_mcp/server.py, android/.../DjCommandClient.kt.
- **P2b is gated:** net-new Media3 queue ops — plan-back sent (#5214).

## P2b done (2026-07-03) — queue/play_next/reorder + queue visibility (approved via #5214/PROCEED #1971)
Jarvis answers: (1) resolve track_ids client-side; (2) payloads as specified; (3) start-playing on empty queue (play_now fallback); (4) Option A — expose a lightweight queue list in now-playing state so reorder is real (C rejected: breaks on duplicate tracks).
- **Server:** schemas.py new `NowPlayingQueueItem` + `PlaybackState.queue: list[...] = []`; playback.py get_state default `queue: []`. No bus/arch change (bus stores the whole dict). Tests still 214 pass.
- **MCP:** new tools dj_queue, dj_play_next, dj_reorder (+ `_enqueue` helper). dj_now_playing now lists the full queue with indices and a `>` marker on the current track. All 6 tools register.
- **Android:** PlaybackManager new `enqueueTracks`/`playNextTracks`/`moveTrack` (net-new Media3 ops: addMediaItems, addMediaItems(index,...), moveMediaItem) + `toDjQueueItem`. moveTrack keeps currentIndex pinned to the playing item. DjCommandClient dispatches queue/play_next/reorder, `resolveTracks` helper, reportLoop now publishes the queue list. ApiModels: DjCommandPayload +from_index/+to_index, new QueueTrackDto, PlaybackStateDto +queue. BUILD SUCCESSFUL (versionCode 22 APK).
- **Smoke-tested green (fresh server):** play_now[201,202] → queue[203] → play_next[204] (insert after current → 201,204,202,203) → reorder(1→3) → final 201,202,203,204; dj_now_playing lists all 4 with indices. On-device Media3 execution still = Todd's phone test.
- ⚠️ Gotcha burned earlier: a stale uvicorn kept squatting the port so "restarted" servers silently hit OLD code. Always `taskkill /F /PID` the port's LISTEN pid (netstat -ano) or use a fresh port when re-testing.

## P3 done (2026-07-03) — dj_queue_by (MCP-side name resolution)
- New MCP tool `dj_queue_by(query, kind, mode, limit)`. Resolves a NAME → tracks entirely in the MCP server over the catalog REST API (no /search endpoint): kind='artist'|'album'|'genre', matched exact > prefix > substring (case-insensitive); mode='now'|'queue'|'next' maps to play_now/queue/play_next. Pure MCP-side — zero client/server changes.
- Helpers: `_get`, `_best_match`, `_MODE_CMD`. 7 DJ tools total now register.
- Smoke-tested against the real catalog (676 artists / 4100 albums / 73810 tracks): artist exact ('Complete Mozart Edition') + prefix ('various'→Various Artists), album ('All Time Top 1000' by Tryout), graceful no-match + bad-mode messages. All enqueue through the bus.

## STATUS: assignment #1970 COMPLETE (STEP 1 + STEP 2 P2a/P2b/P3), all committed.
Only remaining verification is on-device Media3 (Todd installs app-debug.apk and confirms play/skip/queue/reorder physically move audio). Commits: STEP1+P2a=91f8027, P2b=a6225b7, P3=(this commit).

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
