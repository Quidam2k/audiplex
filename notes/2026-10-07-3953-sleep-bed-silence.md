# #3953: sleep timer ended in silence, not the bed (2026-10-07)

- Root cause: the :8100 process started 10/6 21:00:26, but /api/library/sleep-beds landed at 22:15 (44b68a0). The app's loadSleepBeds() swallowed the 404, so the dialog offered only Silence, preselected. Confirmed: the route returned 404 on the old pid 66244.
- Fix live: `python Q:/Pantheon/scripts/restart_services.py --only audiplex` (supervisor, new pid 68684). An authed GET /sleep-beds returns 200 [610 Starship, 609 Brown Noise]. Cleared dp 745/600/647 (all :8100 restarts; their commits are ancestors of 5d7c21a).
- 6aff09d: bed-load error + Retry, last-used bed preselect (SettingsStore), "Start (silence)" label, "Test: 1 min" chip (15 s fade), loop extracted verbatim to SleepFade.runTimer + SleepTimerHandoffTest (virtual time). 121/121 unit tests.
- Held APK 1.0.58 in data/held/3953 (built from a tree copy; live counter bumped to 59; the updater still serves 1.0.57). Not run on a device.
- Open: the bed is a streamed loop with no retry, so a network drop overnight stops it.
