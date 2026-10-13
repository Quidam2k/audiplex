# Player feast (asg 7958 / 7959 / 7962), 2026-10-13

## Shipped (branch duck-deeper)
- 8f4f46d #4114: `dj_pool_set(start_now=True)` skips to the first pick and resumes in the same call. A failed or unconfirmed trim sends nothing (test_4114_pool_start_now.py). Live when the persona MCP reloads. Pantheon follow-up: #4358 (bike_dj_mode START text).
- cf30737 #4217/#4219/#4220/#4221: APK source only. Stream cache (all 610 books have the moov at the end), chapter seek bar, long-press skip size, gear everywhere. Compiled, unit tests 160/160. NOT built (deploy_pending #891).

## Findings
- 10/9 ride: 3m56s from leaving to music. 2m52s passed before any persona action (Pantheon #4101 fixed that path afterward, unverified on a bike) plus a 38 s LLM gap (now start_now).
- Music levelling (normalize_music, -20 attenuate-only) is why the music is quiet (#4117/#4118). Todd's pick is on the Review card's music section. deploy_pending #887 (todd_decision) guards the :8100 restart.

## Post-trip (after 10/22)
1. Build the APK (optionally apply notes/4222-book-boost-app.patch first; it still applies) and verify per dp #891.
2. Apply Todd's music pick (dp #887) before or with the :8100 restart.
3. Live-tree test leak, now bigger: with `server/data/todd_stop.json` latched, 21 server tests fail in the live tree (playback_devices/registry/api_playback/dj_triggers/3504/3505) plus test_3249_diag_log. All pass in a clean checkout. Isolate the latch path in conftest.
