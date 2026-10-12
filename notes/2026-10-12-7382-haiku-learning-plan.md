# #7382: Haiku 5.5 learning pass (PLAN ONLY, nothing runs, no key wiring)

Approved as plan only (plan-back 29190, PROCEED #7893). Nothing here is built.

## What it adds on top of what is shipped
The deterministic pass already runs at ride end: #4057 `dj_learn` (skip/played-through/loved
weights), the #3981 quiet-bit scanner (track-profile intro/fade), #3984 song memory (sourced
facts and links), and the #4056 pool tilt. What it can't do is *explain* a ride: why a song
worked after another, or which themes Todd leaned into. The Haiku pass writes those notes.

## Shape
- Trigger: after `POST /api/dj/learn` succeeds for a ride (same since/until, same ride_id), as an
  idle-gated background job. Never in the pick path, and never while music plays.
- Input per ride (one request): the ride's ordered plays (artist, title, played_seconds,
  skip/complete, todd_asked), the #4057 weight changes, and for each played track its sourced
  #3984 facts (RFL notes, verbatim with source). No lyrics are sent; only stored facts.
- Output (structured outputs, JSON schema): up to ~10 candidate notes:
  `{track_a, track_b|null, kind: pair|theme|context, text, evidence_ids[]}`. A note with no
  `evidence_ids` from the input is dropped (no free-floating trivia).
- Storage: RFL `dj_notes.db` rows with `origin='llm'`, `model='claude-haiku-5-5'`, ride_id.
  song_context ranks `origin='llm'` BELOW every sourced kind: a weight of 1 against the
  WEIGHTS table's sourced kinds, never a veto, and never on its own enough for after()
  (it is added to WEAK_KINDS). Todd can wipe them per ride (`DELETE ... WHERE ride_id=`).
- Never: change weights, bans or loves, or write anything song_context treats as "never after".

## Model and cost (claude-api skill table, cached 2026-10-06)
- `claude-haiku-5-5`: $0.10 / MTok in, $0.50 / MTok out (prompts up to 100K). Batch is 50% off.
  `output_config.effort: "low"`, `max_tokens` ~4000.
- One ride is about 40 tracks x ~300 tokens of facts plus ~2K of instructions, so ~15K in and
  ~2K out: about $0.0015 + $0.001, or **~$0.003 a ride** (~$0.0013 batched). 100 rides is ~$0.30.
- Re-measure with `messages.count_tokens` on a real ride before turning it on.

## Decisions Todd/Jarvis need to make before it runs
1. API key: Pantheon strips ANTHROPIC_API_KEY. Options: an `ant auth login` profile for the
   Audiplex service account, or a dedicated key in Audiplex's own config (not Pantheon's env).
2. Whether llm notes are ever spoken in bridges (proposed: no, at first. They only tilt picks
   and show in `dj_song_context`, labelled "(model guess)").
3. Retention: keep, or expire llm notes that a later ride contradicts.

## Build steps (later, not now)
1. `audiplex/dj_reflect.py`: build the input from play_stats + dj_learn result + song_context.
2. SDK call (`anthropic` in the server venv; structured outputs; refusal check; fail-closed).
3. RFL store write with origin='llm'; song_context WEIGHTS/WEAK_KINDS entry.
4. Tests: replayed 10/8 ride with a stubbed client; evidence-less notes dropped; llm-only links
   never surface from after().
