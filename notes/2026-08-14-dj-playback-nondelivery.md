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

## 08-17 (later) — planning session for the FGS bundle (#3022/#3021/#3024)

Fresh worker, plan-back #9210 submitted. Three findings that reshape the work:

**1. `targetSdk` is 34, so the always-on FGS is legal today — with a tripwire.**
Android 15's 6h-per-24h timeout on `dataSync` foreground services is gated on
targetSdk 35+. At 34 we are exempt even on a Pixel running 15/16. The day
anyone bumps targetSdk to 35, the DJ link starts dying after 6 cumulative
hours a day, silently, and it will look like a regression somewhere else.
Recommendation: use the `specialUse` FGS type, which has no timeout at any
targetSdk, rather than `dataSync`.

**2. There is no track rating anywhere — `dj_rate` rates RECOMMENDATIONS.**
`dj_rate`/`dj_taste` operate on a `recs` table in MCP-side SQLite
(`audiplex_mcp/data/dj/taste.db`), deliberately kept out of `audiplex.db`
because a recommendation has no track row to hang off. The audiplex server has
no rating model, table or endpoint at all. So #3024 is a new store + endpoints
+ UI + MCP surface, not a UI-over-existing-store job. Note also
`audiplex_mcp/server.py` ~1029, which deferred app-side rating UI as costing
"a build, a versionCode bump and an in-app update round-trip ... before we
know the signal is worth anything" — Todd's ask overrides that, and the APK
cost is being paid for the FGS anyway.

**3. The client log is in-memory only and the trace shipper will race it.**
`playback_bus`'s client log is a 200-entry ring buffer with no persistence,
and the exit watermark advances on delivery. Ship #3021 as-is and a server
restart between delivery and reading destroys the 08-14 trace permanently —
watermark says shipped, Android rolls the record off, artifact gone. Fix
(persist `process_exit` entries to disk) belongs in the same phase as the
trace shipper, not after it.

**Blocked, needs Jarvis:** could not read the live client log — the endpoint
needs a Bearer token and minting one from the config `jwt_secret` was denied
by the auto-mode classifier. Not worked around. Asked Jarvis to paste
`dj_client_log`'s `process_exit` entries; that is the last thing between
#3019 and closure, and it also shows whether SIGNALED records really arrive
with blank descriptions before the fix is written blind.

**Design principle for the FGS, from Todd's reversibility requirement:** the
service owns nothing. `DjCommandClient`'s loops stay app-scoped and unchanged;
`DjLinkService`'s only job is keeping the process alive and holding a
notification. Toggle it off and behaviour is bit-for-bit what it is today.
Nothing else may ever depend on it.

Also queued as a near-free win: with the FGS up, a DJ play with the app closed
is the **backgrounded** audio-focus experiment that never ran (see above). If
the FGS is the fix for 08-14, that is the test where it shows.

## 08-17 (evening) — APK 1.0.37 shipped: FGS + trace shipper + star ratings

Plan-back #9210 approved (#3025) with all five rulings, plus #3026 (Todd:
stars, not thumbs). All three phases built, tested and pushed.

**Phase A — #3021, stack traces + three decoding defects.**
`getTraceInputStream()` now ships the first 40 lines (4000-char cap) for CRASH
and ANR. The blank-description bug was real and confirmed on-device: SIGNALED
records carry `""`, not null, so the old `?:` never fired — Jarvis's paste
shows the double-space where the message should be. Status is now decoded to a
signal name, because "SIGNALED" repeated four times says nothing while
`SIGKILL` does. Watermark key → v3 so the retained history re-ships WITH
traces.

**Server-side durability (finding 3).** `process_exit` entries are now appended
to `server/data/client-exits.jsonl` as well as the ring buffer, exposed via
`GET /api/playback/client-exits` and `dj_client_exits`. Without this the first
trace we ever capture could be destroyed by a restart, since the phone
advances its watermark on delivery and never re-sends.

**Phase B — #3022, the always-on FGS.** `DjLinkService`, type **specialUse**
(the targetSdk-35 dataSync timeout trap is documented in the manifest itself).
It owns nothing: `DjCommandClient`'s loops are untouched in the application
scope, and the service only holds the process up and shows a notification —
so the toggle genuinely reverts to prior behaviour. Started from
`MainActivity` (an FGS start is illegal from a backgrounded
`Application.onCreate`) and from `BootReceiver` on reboot. Notification is
IMPORTANCE_MIN and reports real link state, doubling as the liveness readout
whose absence made the 08-14 test fail silently.

**Phase C — #3024, stars 1-5.** The assignment's premise was wrong: nothing
rated tracks anywhere, so this added the store too — `track_ratings`,
`PUT/DELETE /api/music/tracks/{id}/rating`, `GET /api/music/ratings`, and a
five-star row on the now-playing screen (music only). Tap the set star to
clear; re-rating keeps an existing note.

**A trap worth remembering:** the DJ reads ratings through a SEPARATE
owner-scoped endpoint, `GET /api/playback/ratings`. It authenticates as
`dj-agent`, which has never rated anything, so the per-caller read would have
returned `[]` and the DJ would have concluded Todd has no opinions rather than
that it was querying the wrong account. Same trap the playlists/favorites
reads in that router already avoid — and the same one `dj_taste`'s
most-played read still lives with, deliberately, with a comment saying so.

**Verification:** server 294 passed, Android unit tests green, APK **1.0.37**
built (device is on 1.0.36, so the in-app updater will offer it). Server on
:8100 RESTARTED onto the new code — `/api/playback/client-exits`,
`/api/playback/ratings` and `/api/music/ratings` all answer 401 (route present,
auth required) rather than 404, and `track_ratings` exists in the DB. No
active connections were open at restart time, so nothing was interrupted.

**Not device-verified** — that needs Todd's install. Two one-time phone
settings: allow the notification, and set battery to Unrestricted (the same
treatment Tailscale already needs on this device) or Doze will cut the link's
network anyway.

**The first post-install test does double duty** (Jarvis endorsed): a DJ play
with the app CLOSED is both the FGS liveness proof and the backgrounded
audio-focus experiment that was never run. If focus was the 08-14 cause, this
is where it shows — and if the FGS fixed it, it shows there too.

## Carry-forward for the next session (jarvis #3028)

**Do NOT fix ad hoc — file it in the next plan-back:** `dj_taste`'s
library-side reads (`/api/music/most-played`, `/api/music/likely-skips`) have
the same caller-scoping trap the ratings read avoided. They are per-caller,
the DJ calls as `dj-agent`, and `dj-agent` has never played anything — so
those lists are always empty for the DJ. The existing comment there says so
honestly rather than pretending otherwise, which is why this is a known
limitation and not a bug, but the fix is the same shape as
`GET /api/playback/ratings`: an owner-scoped read in the playback router.
Jarvis's ruling is that it goes through plan review, not a drive-by patch.

Also on record from #3028: the owner-scoped ratings deviation is APPROVED as
the dud-prevention standard, and NOT converting `dj_rate`'s good/meh rec
vocabulary to 1-5 was the right call — converting would have corrupted
existing rec history for nothing.

Install timing: Todd installs after his 5 PM block, so **no install
verification tonight before then**. Post-install analysis belongs to a fresh
session.

## 08-18 morning — trace verification (#3031). THE TRACE IS NOT THERE.

Worker: worker-audiplex-push-playback--20260818-142216-e204. MCP `dj_*` tools
are not connected in this session, so this was read from the durable artifact
directly: `server/data/client-exits.jsonl` (the file `dj_client_exits` serves).

**1.0.37 is installed and the v3 re-ship happened.** Entry 20 is a
`killDueToPackageUpdate` at 2026-08-18 01:04:59Z (= 08-17 18:04 PT, matching
the file mtime), and the batch that follows re-ships all 16 records Android
still holds — exactly the watermark-v3 behaviour #3021 designed.

**Zero of the 16 records carry a `trace` field. Including the CRASH.** Not a
truncated trace, not a blank one — `detail()` only emits the key when the
trace is non-blank, and the key is absent everywhere.

The CRASH record is nonetheless a real result:

| field | value |
|---|---|
| `at` | 1786723385727 = **2026-08-14 16:03:05Z** |
| reason | CRASH |
| importance | 100 (FOREGROUND) |
| trace | **absent** |

That timestamp lands inside the 16:02:58–16:03:03 window the 08-14 analysis
inferred for process A's death. So **root cause 2 is now confirmed by record
rather than by inference: process A died of an uncaught exception (CRASH), not
LOW_MEMORY and not the FGS-promotion exception that was the leading
hypothesis.** What it does NOT give us is *which* exception, because the stack
is missing.

**Why the stack is missing is undetermined and cannot be settled remotely.**
Two candidates, indistinguishable from the server: (a) Android retained no
trace for this record — `getTraceInputStream()` is documented around ANR and
native tombstones, and a 4-day-old record's trace file may simply be pruned;
or (b) `readTrace()` threw and its `getOrDefault("")` swallowed it. The code
cannot tell us which, because both paths produce an identical empty string and
nothing is logged. That is an instrumentation gap in #3021, and it is the
thing worth fixing — see the plan-back.

**The 08-14 stack is realistically gone.** The phone advanced its watermark
past this record on delivery, and the record is one of only 16 Android
retains, so it will roll out. Recovery is not a plan; making the *next* crash
self-explain is.

### Overnight link continuity: NOT auditable, and nobody should claim it is

`PlaybackBus._last_poll_at` is a single float in process memory. There is no
poll history, and the server restarted overnight, so "continuous beat vs
gaps" cannot be answered from the server at all — only the two point samples
jarvis already has (a state report ~5:50 AM, a poll 6s before 7:26 AM).

There IS one piece of real evidence the FGS held, and it is stronger than
those samples: **`client-exits.jsonl` has not been appended to since the
01:04:54Z install batch.** Any process death would have been reported on the
next launch and landed in that file. No new entries = no reported death in
the ~13 hours since install. Caveat: a death whose report failed to deliver
would look identical, so this is strong but not conclusive.

Battery-Unrestricted on the Pixel remains unconfirmed by Todd.

### Two design findings that change Phase 3's shape

**1. The WS doorbell buys almost no latency.** `bus.enqueue()` puts to an
`asyncio.Queue` that a parked `bus.next()` is already awaiting, so a command
reaches a *connected* device essentially instantly today — the long-poll IS a
push. The only exposed gap is the sub-second re-issue window after a 204. The
original #900 framing ("stop hanging out in a polling loop") was aimed at
waking a **frozen or dead** app, and that is precisely what the FGS now
solves. So the doorbell's justification is largely absorbed.

**2. The ACK is the part that actually mattered.** `bus.next()` pops the
record before the HTTP response is written (root cause 5, still present), and
`dispatch()` can then drop it silently — e.g. `resolveTracks()` returning
empty just `return`s, consuming the command with no trace anywhere. The DJ
still cannot say "the device got it and acted on it", which is the exact
ambiguity that made the 08-14 test fail silently.

## 08-18 — Phase 3 built and shipped (#3032 PROCEED, plan-back #9245)

All four rulings applied: WS doorbell DEFERRED (latency evidence above), ACK
at-least-once with N=60 and client dedup, 3a/3b split, no blocking on the
battery question.

### 3a — server only, live on :8100 (e95b7c1, 413c2d1)

The `asyncio.Queue` is gone. Commands live in a bounded registry with a status
(`queued` → `delivered` → `acked`/`failed`); delivery MARKS, it no longer
consumes. **Root cause 5 is closed.** Un-acked deliveries are re-offered after
60s and `/command/next` returns `delivery_count` so a redelivery is visible on
both sides.

New: `POST /api/playback/command/{id}/ack`, `GET /api/playback/commands`,
`GET /api/playback/link-history`, plus MCP `dj_command_status` and
`dj_link_history`. `dj_play_now` now points the caller at
`dj_command_status` rather than implying the track played.

**#3028 closed.** `/api/music/most-played` and `/likely-skips` filter by the
CALLING user, and the DJ calls as `dj-agent`, which has never played anything —
so `dj_taste`'s reads were always `[]`. Owner-scoped copies now live in the
playback router (the `/api/playback/ratings` shape) and `dj_taste` reads those.
Server-only, no APK, as argued. A test pins the per-caller endpoints still
returning `[]` for `dj-agent`, so the distinction stays deliberate.

314 server tests pass. Every endpoint and every new MCP tool was exercised live
over HTTP against a scratch server on :8199.

**A bug the live check caught, worth remembering.** After restarting :8100, the
production `link-history.jsonl` held a dozen `resumed` entries in sub-second
bursts — mine. Every test that polls `/command/next` writes a link event, and
`bus.reset()` clears the once-per-process guard. The exit log had been
protected by an autouse conftest fixture since #3021; the link log shipped
without the equivalent, and the fixture I added covered only its own file.
Isolation now lives in conftest for the whole suite, verified by a full run
leaving both files untouched. The bogus rows are purged.

**Unplanned evidence:** the one surviving entry is the phone re-attaching at
07:41:34 after that restart — live proof the FGS recovers across a server
restart, and now a durable record instead of something someone has to witness.
Note the limit: link history only accumulates from 08-18 onward. It cannot
retroactively answer last night.

### 3b — APK 1.0.38, built, NOT yet installed (a4d61d8)

Every exit path from a DJ command now acks. Previously `dispatch()` returned
`Unit` and half its branches bailed with a bare `return`: an unresolvable track
id, a payload missing a field, an unknown command type, or a thrown exception
all produced *nothing*, against a server that had already destroyed the command
by delivering it. Outcomes are `ok`, `no_tracks`, `bad_payload` (names the
field), `unknown_type`, `error`. A partly-resolved track list acks `ok` **with
detail**, because a half-loaded queue reported as a clean success is how a
library gap stays invisible. The client remembers the last 64 command ids and
re-acks a redelivery rather than executing it twice.

`trace_status` (present / none / `error:<class>`) is now on every exit record,
and the watermark goes to **v4** so the retained history re-ships once carrying
it.

Android unit tests green, 11 new. The server's update endpoint already serves
1.0.38; the device is on 1.0.37.

### ⚠️ THE ONE THING STILL OPEN — the 08-14 CRASH verdict

Per jarvis (#3032), the v4 re-ship's answer for the 08-14 CRASH — `none` vs
`error:<class>` — is to be written here as this file's **closing entry**.

**It cannot be written yet, and must not be guessed.** The answer does not
exist until Todd installs 1.0.38 and the phone re-ships its history. Whoever
picks this up: read `dj_client_exits` (or `server/data/client-exits.jsonl`)
after that install, find the record at `at=1786723385727`, and record its
`trace_status`:

- `none` → Android never kept a stack for that record. The 08-14 exception is
  gone for good; close it as unrecoverable and rely on the instrumentation for
  the next one.
- `error:<class>` → our read was broken all along. Fix that class of failure;
  the same bug would have eaten every future trace too.

Either way it is a real endpoint for a record that has been chased for four
days. Note the crash may already have rolled out of Android's 16-record
history, in which case it simply will not appear — say so plainly rather than
reporting a verdict that was never observed.
