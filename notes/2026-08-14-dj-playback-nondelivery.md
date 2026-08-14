# DJ playback non-delivery + 1-second stop (#2961 / #2962, suggestion #900)

Worker: worker-audiplex-push-playback--20260814-162846-cc0a
Date: 2026-08-14

## Incident

- 15:58Z Jarvis queues `dj_play_now` (command #1, tracks 65 Barracuda + 55 The Chain). No collection, no now-playing state.
- ~16:03Z Todd launches the app manually: hears ~0.75s of Barracuda, then silence.
- 16:04Z `dj_now_playing` still reports "nothing playing".

## Evidence gathered (server-side; phone not reachable over adb)

| Source | Finding |
|---|---|
| `GET /api/playback/state` (16:32Z) | `{playing:false, track:null, volume:1.0, updated_at:2026-08-14T16:03:08Z}` — one report, nothing in the 29 min since |
| `play_stats` table | rows 1 and 2: track 65 `start` at **16:02:57.834** and **16:02:57.837** (3 ms apart, duplicate) |
| `GET /api/music/stream/track/65` | 206, `content-range: bytes 0-65535/6363693`; open-ended range returns all 5,315,117 remaining bytes. Server streaming healthy, file intact |
| `adb devices` | none attached — no logcat available |
| build | versionCode 33; no `google-services.json`, no Firebase deps (FCM is net-new) |

## Root causes

**1. State reporting is dead the moment anything plays (confirmed).**
`DjCommandClient.reportLoop` runs on `Dispatchers.IO` and calls
`playbackManager.playerVolume()` → `controller?.volume`. A Media3 `MediaController`
is thread-confined to the looper it was built on (main, via `ensureController`
under `withContext(Dispatchers.Main)`); reading `.volume` off that thread throws
`IllegalStateException`. The loop has no try/catch, so the job dies — and because
the scope is a `SupervisorJob`, it dies *silently* without touching `commandLoop`.
While `controller == null` (nothing has ever played) the call is harmless
(`?: 1f`), which is exactly why the only state ever recorded is an idle one with
`volume: 1.0`. Every state report after the first playback is lost, forever, until
process restart.

**2. Two app processes — the first one died (confirmed by inference).**
Playback demonstrably started at 16:02:57 (play_stats), which requires
`ensureController` to have connected → `controller != null` in that process. But
the 16:03:08 report carries `volume: 1.0`, i.e. `controller == null` → a *different,
freshly started* process. So process A died between 16:02:58 and 16:03:03. The
audio stopped because the process hosting ExoPlayer went away, not because the
stream failed.

Leading hypothesis for the death (unconfirmed, needs `dumpsys activity exit-info`):
cold-start foreground-service race. The DJ command is dispatched from
`Application` scope ~2 s into launch, plausibly before `MainActivity` is RESUMED;
Media3's `MediaSessionService` foreground promotion then hits
`ForegroundServiceStartNotAllowedException`.

**3. No client-side error visibility at all.**
No `Player.Listener.onPlayerError` override anywhere in the app. Every API call in
the DJ path is wrapped in `runCatching {}` with an empty handler. No client log
shipping. A silent process death or ExoPlayer error is undiagnosable remotely —
this is the reason today's incident needed forensic reconstruction from play_stats.

**4. `dj_now_playing` cannot distinguish "no device" from "idle" from "stale".**
It prints "Nothing is playing (no now-playing state reported yet)" whenever
`track` is null, and never surfaces `updated_at`. Identical text for a dead device,
a live idle device, and 30-minute-stale state. This is the observability gap that
misled the 15:58 test.

**5. Command delivery has no ACK and no redelivery.**
`playback_bus.next()` uses `asyncio.Queue.get()`, which pops the record before the
HTTP response is written. A client disconnect mid-response loses the command
silently. Queue and state are process-memory only; a server restart drops both.

**6. Duplicate play-stat `start` events.**
`playTrackList` posts a manual `start` *and* `onMediaItemTransition` posts one for
the same index — two rows per track start, polluting the taste loop.

## Shipped (plan-back #8963 approved by jarvis + karen, PROCEED #2964)

Phases 1 and 2. Phase 3 (command registry + ACK + WS doorbell) is approved as
designed but not started; its standby half is waiting on Todd's ruling.

Server:
- `GET /api/playback/device` — device liveness. The bus now records last poll,
  last command delivered, last state report. A poll is the liveness beat: the
  client re-issues it every ~25s whether or not anything is playing, so it
  proves a player is alive even when nothing is loaded.
- `POST`/`GET /api/playback/client-log` — 200-entry in-memory ring buffer for
  diagnostics shipped up by the phone.
- `PlaybackBus.reset()` so tests stop reaching into private attrs.

MCP:
- `dj_device_status` (new) — connected / last poll / last command / last report.
- `dj_client_log` (new) — reads player errors and process-exit reasons.
- `dj_now_playing` — now says whether a player is CONNECTED and idle vs nothing
  listening, and warns when the snapshot is over a minute stale.
- `dj_play_now` — reports whether a player is actually connected instead of
  "the device plays when it next polls".

Android:
- `PlaybackManager.playerVolume()` returns a cached snapshot refreshed on the
  main thread instead of touching `controller.volume`. **This is the fix for
  root cause 1** — the off-thread read that killed the report loop.
- `reportLoop` per-tick try/catch; a failed tick is reported to the client log
  instead of silently ending the loop.
- `onPlayerError` override → ships the error code, cause and track to the server.
- `ClientLogReporter` (new) — also reports `ApplicationExitInfo` on startup, so
  the *next* silent process death explains itself without adb. Watermarked in
  DataStore so each death reports exactly once.
- Duplicate `start` play-stat suppressed (whichever of the two paths claims the
  index first owns it).

## Verification

- Server suite: 283 passed, including 7 new tests for device liveness and the
  client log.
- Endpoints exercised live against a scratch server on :8199 — enqueue → poll →
  delivery → device status → client log all correct.
- All four changed/new MCP tools rendered against that server.
- `assembleDebug` green (APK versionCode 33 / 1.0.33); `testDebugUnitTest` green.
- NOT device-verified: the phone is not reachable over adb. Confirmation is
  Todd installing the APK, then `dj_device_status` showing connected and
  `dj_now_playing` continuing to update after playback starts — which is
  exactly what root cause 1 prevented.

## Still open

- Todd's ruling on the standby mechanism (foreground DJ-link service vs FCM).
- Why process A actually died. Unchanged and still unconfirmed — but the app now
  self-reports it, so the next occurrence answers it.
- Deploy: server restart (new endpoints) + APK install on the Pixel.
