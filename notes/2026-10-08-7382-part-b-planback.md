# #7382 Part B plan-back (queued: orch allows one open plan-back; submit after 27452 signoff)
DONE = bridges carry RFL notes with no persona step, a ride-end review runs on a replayed ride and changes weights, and the chip shows the live book state.
## Findings (Explore agent 10/8)
- RFL notes ALREADY on [DJ BRIDGE] (Pantheon dj_bridge_push.py:320-361 rfl_notes via RFL store.py). Gaps: intro-mode `next` no notes; [BIKE DJ START] (bike_dj_mode.py build_text 203-254) no track notes.
- Kate Bush bug: RFL src/notes/store.py:350-357 title-only fallback + original_artist_facts (201-217) accepts if ANY fact names playing artist; header prints stored row's artist. Deliberate in RFL e74c9cf. Pantheon duplicates at dj_bridge_push.py:335-344.
- Quiet-bit scanner BUILT (Audiplex b054a2c, GET /api/playback/track-profile/{id}); not fed to bridges. Pantheon notes/2026-10-08-dj-already-built-audit.md is stale on this and on sets.
- Skip data: play_stats (start/complete/skip, played_seconds), taste.py (#947, early skip <10s), dj_love, dj_bans. dj_pool random.choice, no weights.
- Bike OFF: data/runtime/bike_dj_state.json ended_at; hook bike_dj_mode.sweep() 676-700.
- Chip: app_control.whats_playing (893) ignores book; /api/playback/state has book{} + position_ms; copy context_status_hook._device_line(). No pause+bookmark endpoint. "pause your book" at audiplex_mcp/server.py:372-378, app_control.py:535/1030.
## Build
1. #4056: (a) RFL lookup: title-only cross-artist match returns only original/cover relationship facts labelled with PLAYING artist; test Kate Bush. (b) dj_bridge_push uses fixed lookup, intro-mode attaches next. (c) BIKE DJ START attaches first queued track notes + set name. (d) bridge payload adds intro_quiet_s/outro_fade_s from track-profile. (e) Haiku 5.5 enrichment PLAN ONLY (origin='llm', ranked below sourced; key decision needed — Pantheon strips ANTHROPIC_API_KEY).
2. #4057: Audiplex dj_learn.py + dj_track_weights(track_id, weight, reason, updated_at, ride_id); sweep END -> POST /api/dj/learn {since,until}. fast skip <5s x0.5; skipped on 2+ rides -> 0 (soft drop, reversible via dj_love/clear, reason stored); played-through x1.1; loved x1.3; clamp 0-2. dj_pool random.choices weighted; 0 excluded like ban, listed in status. Post 3-line "what I learned". Replay test.
3. #4052: whats_playing adds book; context_status_hook dedup line; Audiplex POST /api/playback/pause-book (pause + PUT progress); dj_play/dj_pool_set call it when a book plays; drop "pause your book" line for Audiplex.
Q1 OK to edit RFL + Pantheon via orch_safe_commit? Q2 Haiku key stays plan-only.
