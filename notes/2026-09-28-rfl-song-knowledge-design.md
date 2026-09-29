# Design: RFL song knowledge in `dj_break_brief`

Status: design only, 2026-09-28. No code changed. (Codex draft unavailable: over quota; written by hand.)

## Read this first: RFL's analysis is mostly placeholder
`track_analysis` (3508 rows, `radio_free_luna.db`) looks rich but is not. Verified read-only:
- `summary` (2298 non-null) is always the template "A <Genre> track by <Artist>".
- `cultural_context` is "Analysis not available" in every row; `notable_elements` is `[]`.
- `tempo`, `key_signature`, `loudness` are NULL; `mood_valence` 0.0 / `energy_level` 0.5 are defaults.
- `themes` are genre-derived templates (Rock -> ["energy","rebellion","freedom"]).
- Real data: `lyrics` (1210 rows) and, from the joined `tracks` table, `year`, `genre`.
- `track_analysis` has no artist/title. Join `track_analysis.track_id = tracks.id`.

So the smallest slice uses only year, genre and one lyric line. Everything else is skipped.

## Flag
`RFL_KNOWLEDGE_ENABLED` (prose: `rfl_knowledge_enabled`), env var, default off, read at
call time so it toggles without restart. Optional `RFL_DB_PATH`, defaulting to
`Q:/Development/radio_free_luna/data/radio_free_luna.db`.

## Slice
When on, `dj_break_brief` looks up the current track and the next queue entry (both already
in `/api/playback/state`) and appends a 2-line block after the Previous / Now playing / Next lines:
```
Notes: Waiting On An Angel (Ben Harper, 1994, Rock).
       Opens: "Well I was born an original sinner." (Missionary Man, Eurythmics)
```
Per track at most 2 lines total across the block: line 1 = facts from `tracks`
(`title`, `artist`, `year`, `genre`; omit NULLs); line 2 = first non-empty `lyrics` line
(cap ~80 chars, one short quote only) when lyrics exist. Real rows: id 2 gives
"Waiting On An Angel / Ben Harper / 1994 / Rock" (no lyrics); id 24 "Missionary Man /
Eurythmics" has lyrics but NULL year/genre. Never emit `summary`, `themes`,
`cultural_context`, `notable_elements`, tempo or mood.

## Matching (conservative, same spirit as `rfl_import.py`: a blank beats a wrong artist)
Normalize both sides: casefold, strip accents, drop `[...]`/`(...)` tags ("Official Music
Video", "Remastered"), drop "feat./ft." tails, drop leading track numbers ("205 - "),
strip punctuation, collapse spaces.
- Match on (artist, title) equality after normalization, read-only.
- Audiplex titles are often yt-dlp filenames with no artist: if artist is blank, try
  splitting the title on " - " into artist/title; if still blank, accept a title-only
  match only when exactly one RFL row has that normalized title.
- Also try swapped artist/title (RFL has rows like title "Mungo Jerry", artist "In The Summertime").
- Ignore RFL artists "Soundtrack" / "Various Artists" (67 rows) as match keys.
- RFL has 370 repeated (artist,title) groups: if candidates all share identical
  year/genre/lyrics use the first; otherwise ambiguous -> no match.

## Failure modes (all silent: brief unchanged, debug log only)
- DB file missing, locked, corrupt or schema mismatch: catch all `sqlite3.Error`/`OSError`;
  open with `file:...?mode=ro` URI and 0.25 s timeout.
- No match: no Notes for that track.
- Ambiguous match: no Notes for that track.
- Flag off/unset: code path not entered; no DB touch.
A lookup must never raise into the brief or add noticeable latency.

## Test plan (fixture DB with the two real tables, built in a tmp path)
1. Exact match: "Waiting On An Angel"/"Ben Harper" yields line 1 with 1994, Rock.
2. yt-dlp title "Eurythmics - Missionary Man [Official Video]", blank artist: split,
   normalize, match; lyric line included.
3. Ambiguous: two rows same title, different artists, blank artist -> brief unchanged.
4. Flag off, and flag on with missing DB path -> brief byte-identical to baseline.
Plus placeholder guard: a row with template summary/themes never leaks them.

## Out of scope / later
- Re-running real RFL analysis to replace placeholder fields (the actual fix; then summary,
  themes, tempo, mood become usable).
- Caching, fuzzy matching, crosswalk of Audiplex track ids to RFL ids, writing back.
- Quoting lyrics beyond one short line (copyright; prefer paraphrase).
