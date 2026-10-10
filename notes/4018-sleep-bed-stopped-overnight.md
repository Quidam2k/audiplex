# #4018 Sleep bed stopped overnight (2026-10-09/10) — cause + fix plan

## What happened (phone telemetry, server/data/*.jsonl, times PT)
- 01:06 Audiplex updated to 1.0.60 (killed for the package update).
- 01:11:55 book played, sleep timer armed. 01:27-01:29 book faded 0.99 -> 0.025; 01:29:10 "paused by sleep_timer". Bed should be at 0.5 from here.
- 01:32:46 last Audiplex report. link-history: gap 01:32:46 -> 05:40:10 (4 h 08 m).
- 01:32:52 the Pantheon companion on the same phone also restarted from scratch (AudioPlayback onCreate). Its polls and heartbeats were otherwise continuous all night, so the phone never lost Wi-Fi.
- 05:40:10 next Audiplex event = a cold app start (position 0, volume 1.0).

## Cause: the process lost its foreground protection at the handoff and Android killed it ~3.5 min later
- The bed is a private ExoPlayer in PlaybackManager.bedPlay (PlaybackManager.kt ~1342). It is NOT the MediaSession player.
- When the sleep timer pauses the book, the session player is paused. Media3's MediaSessionService then drops foreground, and the process becomes killable even though the bed is still audible. At 01:32 Android reclaimed memory (the companion app died 6 s later), and the bed died with the process.
- Same class, second door: PlaybackService.onTaskRemoved calls stopSelf() when the book is paused, so swiping Audiplex away also kills the bed.
- Also missing on the bed player: setWakeMode(WAKE_MODE_NETWORK) (no wake/Wi-Fi lock anywhere in the app), any onPlayerError/retry, and _bedPlaying is set once in bedPlay and never follows the real player state. The UI therefore says "Sleep bed is playing" with only Stop, which is why getting back to it was hard.
- Volume: the bed ends at a fixed SleepFade.BED_VOLUME = 0.5 with no user control, and the file is quiet (-26 dB mean).

## App fix (needs an APK build, 1.0.61)
1. Foreground while the bed plays: PlaybackService overrides onUpdateNotification(session, startInForegroundRequired || bedPlaying), so the media notification stays foreground with the book paused. onTaskRemoved does not stopSelf while the bed plays.
2. Bed player: setWakeMode(C.WAKE_MODE_NETWORK). Play from a LOCAL copy: download the bed once to app files and reuse it; stream only on first use. No network needed overnight.
3. onPlayerError -> retry (10/30/60 s), then fall back to the local copy of the brown-noise bed. Each failure is logged to playback-diag.
4. _bedPlaying follows Player.Listener isPlaying/playbackState. If the bed dies, the UI shows "Sleep bed stopped" + "Restart bed" (one tap, same bed, same volume).
5. Bed volume: its own slider in the sleep dialog and on the "Sleep bed is playing" row, persisted in SettingsStore, independent of the book. Default raised to 0.9.
6. Unit tests: SleepFade/handoff tests keep passing; new test that the foreground-required decision is true while the bed plays; bed state follows the player.

## Daytime test (5 min, NOT at bedtime)
1. Audiplex: start any book, tap the moon, pick the bed, choose "Test: 1 min", Start.
2. Wait for the book to fade (about 75 s) and the bed to come up.
3. Swipe Audiplex away from Recents, then lock the phone.
   - TODAY (1.0.60): the bed stops within seconds or minutes. This reproduces last night.
   - AFTER the fix (1.0.61): the bed keeps playing, the (paused) book's media notification stays up, and it is still playing 15 min later.
4. Unlock and open Audiplex: it should say the bed is playing (or "stopped" + Restart bed if it is not). Move the bed volume slider: the bed gets louder/quieter and the book's volume does not change.
5. Tap Stop bed, then play the book: it resumes from where it faded.

## Shipped (2026-10-10, 6c15edb, APK 1.0.61 served)
- All 6 items in. Extra: Media3 1.4.1's late-artwork path (MediaNotificationManager.onNotificationUpdated) re-decides foreground without onUpdateNotification, so PlaybackService wraps the notification provider and re-routes that update while a bed plays.
- Pure rules in playback/SleepBedRules.kt (+13 JVM tests); 147/147 JVM tests green.
- Not verified on a device: no phone/emulator attached. The daytime test above is the device check.
