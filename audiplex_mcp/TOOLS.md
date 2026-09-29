# audiplex-dj MCP — tool catalog

Every tool a persona can call, one line each. Source of truth for "what can the
DJ do?" — Pantheon's `docs/entry-points.md` points here. A test
(`server/tests/test_ride0928_tools_catalog.py`) fails if a registered tool is
missing from this file, so adding a tool means adding its line.

**Talk guard (#ride0928):** every tool that can start audio on an idle player
(`dj_play_now`, `dj_queue`, `dj_play_next`, `dj_queue_by`, `dj_resume`,
`dj_play_stream`, `dj_mix`, `dj_folder`, `dj_pool_set`, `dj_transfer`, `dj_play_book`) is HELD
while Todd is talking or typing (Pantheon `speech_state.json`). They lead with
`RESULT sent= held= skipped_missing=[titles] phone_ack=`.

## Start and steer the music
- `dj_play_now` — play these track ids now, replacing the queue.
- `dj_queue` — append track ids to the end of the queue.
- `dj_play_next` — insert track ids right after the current song.
- `dj_queue_by` — resolve an artist/album/genre/folder/playlist/favorites/bucket/search NAME (or `tracks`: ids) and play or queue it.
- `dj_mix` — build or re-plan a balanced, shuffled mix from several sources; never interrupts the current song.
- `dj_mix_status` — did dj_mix's song-boundary swap (old phone builds) fire yet?
- `dj_folder` — one folder: shuffle it (via dj_mix), queue it, or save it as a playlist.
- `dj_play_stream` — play an external HTTP stream (e.g. Radio Free Luna).
- `dj_skip` / `dj_previous` — next / previous item in the queue.
- `dj_pause` / `dj_resume` — pause / resume (resume is talk-guarded).
- `dj_seek` — jump to a position in the current track.
- `dj_volume` — app player volume 0-100.
- `dj_reorder` — move a queued item by index (indices from dj_now_playing).
- `dj_upcoming` — the upcoming queue in order, with each track's `#index` and mix source.
- `dj_remove` / `dj_insert` / `dj_swap` — edit what's still to come by `#index` (from dj_upcoming) or track id; one replace_upcoming, the current song is never touched.
- `dj_ban` / `dj_unban` / `dj_bans` — never pick a track again (every copy of the recording) in mixes and the pool; reversible; lists who banned what and why.
- `dj_tag` / `dj_untag` / `dj_tags` — mood/vibe tags you apply by ear (never inferred); a tag is also a mix/pool source: `{"kind": "tag", "query": "chill"}`. (#2806)
- `dj_energy_set` — a set ordered by MEASURED energy (0-100): arc rise / peak / wind_down / steady, optional minutes, tags, energy window. Replaces what's after the current song. Unmeasured tracks are left out and counted. (#2806)

## Rolling pool, specs, cues (a whole ride)
- `dj_pool_set` — start the server-side rolling pool from a saved spec or inline sources; arms its cues.
- `dj_pool_status` — lanes, pending cues, chime and outro state.
- `dj_pool_stop` — stop the pool (what's queued keeps playing).
- `dj_pool_lane` — pause, resume or remove one lane of the running pool.
- `dj_spec_save` / `dj_spec_list` / `dj_spec_show` — save, list, show a named mix spec.
- `dj_spec_add` / `dj_spec_remove` — add or remove sources on a spec (re-syncs a live pool).
- `dj_spec_note` / `dj_spec_notes` — add / list cues on a spec (pre-rendered patter at song boundaries).
- `dj_chimes` — Westminster quarter chimes during a pool: on/off, volume, hour strikes.
- `dj_outro` — after the current song, play your spoken outro, then pause (ride end).
- `dj_outro_cancel` — disarm a pending outro.

## Voice
- `dj_break_brief` — everything to write a break: daypart, time, weather, Previous / Now playing / Next, DJ pair notes.
- `dj_announce` — synthesize your copy and queue it as a voice break (held while Todd talks).
- `dj_patter` — turn the bridge watcher's automatic between-song patter on/off.

## What's playing, and is the phone alive
- `dj_now_playing` — current track, queue with indices, liveness, `missing_on_phone`.
- `dj_command_status` — did the phone ack a command, and what did it say.
- `dj_device_status` — is a player connected and polling right now.
- `dj_devices` / `dj_transfer` — list renderers; hand playback to another device (talk-guarded); a playing audiobook follows too (#2680).
- `dj_play_book` — play an audiobook on the PC renderer from the saved position (shared with the phone; talk-guarded).
- `dj_link_history` — when the phone's link dropped and came back (survives restarts).
- `dj_client_log` / `dj_client_exits` — phone diagnostics: player errors, process exits.

## Library: browse and find
- `dj_library` — survey the library (folders, roots, what's untagged).
- `dj_tracks` — list tracks with ids.
- `dj_search` — find tracks by name.
- `dj_set_kind` — mark tracks/folders music | podcast | clip | ambient (only music goes into mixes and pools).

## Todd's listening and taste
- `dj_history` — what Todd actually played, newest first.
- `dj_cooldown` — what he heard recently enough that a pick would repeat it.
- `dj_check_picks` — before queueing: which picks repeat something recent, and why (advisory).
- `dj_track_ratings` — his own 1-5 star ratings from the phone.
- `dj_track_stats` — completion rates and where skips land.
- `dj_pair_note` / `dj_pair_notes` — write / read DJ notes on a track or a track_a -> track_b pairing (kept across rides).

## Themed buckets
- `dj_bucket_list` / `dj_bucket_show` — list buckets; show one.
- `dj_bucket_save` / `dj_bucket_edit` — build or refine a bucket.
- `dj_bucket_load` — drop a bucket into a mix spec as its own lane.

## Discovery (music the library doesn't have)
- `dj_recommend` — propose a track not in the library, logged.
- `dj_rate` — record how a recommendation landed.
- `dj_taste` — what's been learned about his taste.
- `dj_find_candidates` — find downloadable sources for a recommendation.
- `dj_ingest` — download an APPROVED candidate, tag it, rescan.
- `dj_fetch_from_playlists` — fetch a track covered by Todd's standing playlist approval (#3245).

## Sleep engine
- `dj_bed_play` / `dj_bed_stop` / `dj_bed_volume` — the looping sleep-bed layer.
- `dj_sleep_timer` / `dj_cancel_sleep_timer` — fade out and pause the main player after N minutes.
- `dj_sleep_start` — "sleep mode". No args: the playing book fades out after 30 min while the brown-noise bed (found by title in the library) crossfades in; `bed_mode="under"` keeps the bed underneath from the start.
