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

## Field evidence from the 2026-08-14 19:35Z retest (read 08-17)

Todd installed 1.0.33 and the queued command drained. Server-side record:

| Time (UTC) | Evidence |
|---|---|
| 19:35:11 | command #1 `play_now` delivered (device endpoint) |
| 19:35:13 | play_stats row 3: track 65 `start` — **one row, not two** |
| 19:35:16 | state report: `playing:false, track:65 Barracuda, position_ms:0, duration_ms:263522, queue_length:2, volume:1.0` |
| 19:35:16 → 01:27:05 (08-15) | app kept polling for ~5.9 more hours; **no further state reports, no client-log entries at all** |

**Two fixes are proven in the field:**

1. *Report loop survives a live controller.* `duration_ms: 263522` is set inside
   the `ensureController {}` callback, so a MediaController was connected when
   that report was posted. Under the old code `playerVolume()` would have thrown
   at that exact moment and no report could have existed. The Phase 2 fix works.
2. *Duplicate `start` is gone.* One row at 19:35:13 against the pair 3 ms apart
   that morning.

**The primary bug is NOT a process death, and it is not a stream failure.**
The process lived nearly six hours past the command. The queue loaded, the
controller connected, `prepare()` and `play()` ran, the start stat posted — and
three seconds later playback was `false` at position 0, and it never moved
again. Critically, **no `onPlayerError` ever fired**: the app was demonstrably
able to POST (it was polling throughout), so a player error would have reached
the client log, and the log is empty. That rules out stream, network, auth and
decode failures.

Queue loaded + no error + `playing:false` + position 0 is the signature of an
**audio-focus request that was denied or immediately lost**. `PlaybackService`
builds the player with `handleAudioFocus = true`, so a denied request leaves
playWhenReady false with nothing logged anywhere. The same mechanism explains
the morning's 0.75 s of Barracuda as focus *granted then transiently lost*.

**Decisive instrument — SHIPPED in a3fc24b, APK 1.0.36.**
`Player.Listener.onPlayWhenReadyChanged` is the only place the distinction
surfaces: Media3 flips `playWhenReady` back to false with
`PLAY_WHEN_READY_CHANGE_REASON_AUDIO_FOCUS_LOSS` when a request is refused.
That plus `onPlaybackSuppressionReasonChanged`
(`TRANSIENT_AUDIO_FOCUS_LOSS`, for focus lost mid-track to a call or
assistant) now report to the client log with playback state, suppression
reason and track id. The next play attempt confirms or kills the hypothesis.

### 08-17 16:25 — first focus telemetry, healthy path

Todd played the first track of Misplaced Childhood manually and stopped it.
`playWhenReady=true reason=USER_REQUEST` (BUFFERING, suppression NONE,
track 208), then `playWhenReady=false reason=USER_REQUEST` (READY) at his
manual stop. Zero focus-loss entries, zero suppression, no player error.
Focus is available on the device; nothing is permanently holding it. The DJ
path remains untested — this was a manual play.

Also: the phone's clock runs ~8.4 s AHEAD of the server's (client `at` vs
server `received_at`). Do not correlate the two naively.

### The DJ path and the manual path run the SAME playback code

`playAlbum → playTrackList`, and DJ `play_now → playTracks → playTrackList`.
Identical function, identical media pipeline. So a DJ-path-specific failure is
**not** in the playback logic — it is in the caller context: a DJ command
dispatches from an Application-scope IO coroutine via `withContext(Main)`,
usually while the app is BACKGROUNDED, whereas a manual play comes from a
foregrounded ViewModel. A backgrounded app is exactly the case Android most
often refuses audio focus for, and the same context that makes Media3's
foreground-service promotion fail.

**Next experiment — run `dj_play_now` twice:** once with the app foregrounded,
once backgrounded (screen off or another app on top). If foreground succeeds
and background shows `AUDIO_FOCUS_LOSS`, the hypothesis is confirmed *and*
localized in one shot — and the foreground-service standby answer becomes the
fix for the bug, not just for the wake problem. If both succeed, the fault is
elsewhere and the next probe is reproducing with the Pantheon companion active.

### 08-17 16:27 — DJ path tested CLEAN. Do not re-run this test.

Jarvis fired `dj_play_now` for track 65 (Barracuda) **with the app open**.
Client log: `playWhenReady=true reason=USER_REQUEST`, BUFFERING,
suppression=NONE, trackId=65. Audible playback confirmed by Todd; manual stop
30 s later. So the DJ command path is healthy end-to-end under clean
conditions and **the 08-14 19:35 failure did not reproduce**.

Score: the audio-focus hypothesis is **neither confirmed nor dead**. It stays
open specifically for failure-night conditions, where the Pantheon companion's
mic-hot media-ducking is the standing suspect. The instrumentation itself is
proven live in the field, so a recurrence now self-identifies — waiting is a
legitimate strategy.

One precision for whoever picks this up: this was experiment (1) of the
two-part test below — app **foregrounded**. The **backgrounded** variant was
never run, and that was the actual discriminating variable, since a
backgrounded app is the case Android most often refuses focus for. Jarvis has
deprioritized it in favour of the companion-active reproduction (#3023); noting
it only so nobody concludes the foreground/background question was answered.

### Todd's standby ruling (relayed by jarvis, 08-17 16:27, msg 20639)

**Build the always-on foreground DJ-link service.** His framing: *"We can
change our mind later if it ends up not being a good idea. But it sounds like
that's kind of what we're going to need in order to get that kind of feature
set."* So **reversibility is part of the requirement** — the FGS must stay
cleanly toggleable and must not become load-bearing for unrelated features.
Evidence basis is the `LOW_MEMORY` + 4× `SIGKILL` history above. Architectural,
so a plan-back is required before code (#3022).

**What to look for in `dj_client_log` after the next DJ play:**
- `play_when_ready playWhenReady=false reason=AUDIO_FOCUS_LOSS` → hypothesis
  CONFIRMED, focus was refused or pulled. Fix is a focus-retry/observer, or
  reconsidering `handleAudioFocus=true`.
- `playback_suppressed TRANSIENT_AUDIO_FOCUS_LOSS` → something else on the
  phone grabbed focus mid-track (the Pantheon companion is the obvious
  candidate — see the deliberate note about mic-hot media-ducking in
  `setPlayerVolume`).
- `play_when_ready playWhenReady=true reason=USER_REQUEST` followed by silence
  and no error → focus was fine; the fault is downstream and the hypothesis
  is WRONG. Look at the renderer / data source next.
- No `play_when_ready` entry at all → `play()` never reached the controller;
  look at command dispatch, not playback.

## Still open

- Todd's ruling on the standby mechanism (foreground DJ-link service vs FCM).
- Why process A actually died on 08-14 morning. Still unconfirmed. Note the
  19:35 retest was NOT a death, so the two incidents may share one cause (audio
  focus) or be separate.
- **APK 1.0.36 is built and waiting** — the one install Todd needs. It carries
  everything since 1.0.33: the fixed process-exit reporter (2cc2567, waits for
  a usable API client, advances the watermark only on confirmed delivery, and
  resets the watermark key so the next launch re-ships the exit history Android
  still holds including 16:02:58Z), the test pinning that invariant (d9560ca,
  `ShipExitsTest`, 6 cases), and the audio-focus instrumentation (a3fc24b).
  The device is still on 1.0.33, so no exit reasons have shipped yet.
- Phase 3 (command registry + ACK + WS doorbell) still held pending live
  verification, per jarvis #2966.
- Server on :8100 has been running the new code and stable since 08-14.
