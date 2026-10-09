# Open DJ checks: #7381, #7382, #7433 (written for a cold worker, #7440)

The code for all three is shipped and the unit tests pass. What is left needs Todd on the
phone or a real ride. Todd leaves for Yosemite on Oct 12, so run these after his next ride.
Keep each check read-only unless it says otherwise. Starting a pool puts music on his phone,
so do it only when he is starting a ride and has asked for music.

Server: Audiplex on :8100 (`curl -s localhost:8100/api/health`). Audiplex tests:
`py -3.11 -m pytest` from `server/`.

## #7381 Part A: sets (#4054) and the false dry lane (#4051)
Shipped: audiplex 318b3a9 (#4051), 2c8156d (#4054). Tests: `tests/test_4054_sets.py`,
`tests/test_4051_pool_start_starve.py`, `tests/test_5518_buckets.py` (22 passed 10/8).
Left: a live todd-ride-mix re-pool where every lane picks. There is no dry-run path.
1. At ride start, the BIKE DJ START push should name the set. Load it with
   `dj_bucket_load` / `dj_pool_set(spec="todd-ride-mix")`, not a single inline source.
2. `dj_pool_status` reads "set 'todd-ride-mix', N lane(s)". "ONE LANE" here is a failure.
3. `curl -s localhost:8100/api/playback/pool`: each entry in `lanes` has picks > 0 after
   about N picks (N = number of lanes). Rotation is strict, so no lane repeats before every
   active lane has had a turn. A lane at zero picks with an empty "why" is the #4051 bug back.
4. Re-run `dj_pool_set(spec="todd-ride-mix")` mid-ride (this is the 10/8 repro). The
   whole-albums lane must still pick. No lane may report "ran dry: a copy of a recently
   picked recording" for tracks that never played.
5. "Queued this session" must not count sends that were held or refused.
6. Not in this repo: the agy allowlist that caused KAREN_GATE_TIMEOUT #27357 on
   `dj_bucket_load` is in Pantheon `data/.gemini_always_allow.json` and is mirrored by
   `scripts/mirror_agy_allowlist.py`. It has no audiplex-dj entries; a Pantheon worker
   needs to add them. Reported in event 27596.
Close: `closes: #4054, #4051`, assignment_id=7381.

## #7382 Part B: the DJ learns every ride (#4056, #4057, #4052)
Shipped (#7404): Pantheon 25901dc5 (bridge talk fits the intro/fade, up-next notes),
ee6fb4a1 (a title-only cover match keeps only facts naming the playing artist: Kate Bush),
3471ff5b (learn on the ride's real close, never on pause_stop), 8b3d8ab2 (whats_playing
names the book). Audiplex: 395804e, 1c19c17, 04ca544 (learning, love, todd_asked skip),
92e810d, 774a87e (pause-book), b100a6f (bridge payload, first song with notes).
Plan draft: `notes/2026-10-08-7382-part-b-planback.md`.
Ride checks:
1. The [BIKE DJ START] push and each [DJ BRIDGE] carry RFL notes for the next track, with
   no persona tool call. Check the pushes in the persona inbox or orch events for that ride.
2. Kate Bush: `rfl_track_notes` for "Kate Bush - Running Up That Hill" must not present
   Meg Myers facts as the playing track.
3. When bike mode turns OFF, `POST /api/dj/learn {since,until}` runs (Audiplex
   `routers/dj_learn.py`). The `dj_track_weights` rows for that ride change: fast skips
   (<5 s) get weight x0.5, tracks skipped on 2+ rides go to 0 with a reason (reversible via
   dj_love), and played-through or loved tracks go up. A "what I learned this set" note is
   posted. Cold alternative: replay the 10/8 ride window through the endpoint and diff the
   weights.
4. Starting music while a book plays pauses the book and bookmarks it
   (`POST /api/playback/pause-book`). Check that `GET /api/progress/{book_id}` moved, and
   that no "pause your book" line is spoken.
5. The persona status chip shows the live book (title + position) from `/api/playback/state`.
Still open (not shipped): context-aware picks from #4056, the #4052 chip child item, and
the Haiku 5.5 enrichment pass (plan only: no API key yet, do not run the paid part). Close
with remainder= naming these.

## #7433: device check on 0.4.115 (voice level #7402/#7434, suggestion #4058)
0.4.115 has been served since about 17:03 PT 10/8. Todd was still on 0.4.114 at 00:01Z.
Confirm his version first (get_companion_version_status, display_version).
Check Pantheon `device_logs` (code: src/volume_telemetry.py, src/voice_level.py) for a
session on 0.4.115:
1. `vol_change` rows label source=app vs source=user correctly.
2. No restore clamp after Todd raises the volume himself.
3. The #3873 speech offset is applied AFTER the duck: no volume spike before it.
4. No ride auto-volume rows (the drift mode pref defaults to OFF).
Close: assignment_id=7433 with `closes: #4058`. The library-wide LUFS run and a device
loudness check are the remainder on #7387.
