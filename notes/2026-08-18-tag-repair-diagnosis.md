# Tag repair + robust ingest — diagnosis (#3037 / sugg #953)

Worker: `worker-audiplex-tag-repair--20260818-155604-863c`. Investigation only;
no code written. Plan-back submitted, awaiting PROCEED.

## What the library actually is

`q:\music` is a flat dump of **YouTube rips** (yt-dlp), 217 tracks, 2 albums,
2 artists. All 217 files present on disk.

- 207 tracks sit **loose in the root**, collected into one fake album titled
  `music` whose artist is the **empty string**.
- 10 tracks are a real album folder, `Marillion - Misplaced Childhood`, filed
  under artist **"Various Artists"** despite carrying correct `artist=Marillion`
  tags.

## Correction to #9265's premise

#9265 recorded "207/217 tracks have a blank artist tag". The DB *rows* are
blank; the **files are not**. 202/217 carry a non-empty `artist` tag — it is the
**YouTube uploader/channel**, not the performer:

    ©nam = All You Need Is Love (Remastered 2009)
    ©ART = The Beatles - Topic          <- channel, not artist
    ©cmt = https://www.youtube.com/watch?v=_7xMfIp-irg

The blank rows are a **scanner defect**, not missing metadata.

### Three scanner defects (`server/audiplex/scanners/music.py`)

1. `_artist_from_path` returns the album folder's *parent name*. When the album
   folder IS the music root (`q:\music`), the parent is the drive root, whose
   `.name` is `""` → artist `""` for all 207 loose tracks.
2. `_read_track` never reads the `artist` tag at all. Artist is purely
   path-derived, which is why a correctly tagged Marillion album became
   "Various Artists".
3. Loose files in the root are collected into one album named after the root
   folder. There is no per-file artist/title derivation for flat dumps.

## Signal inventory (measured, all 217 files)

| signal | count |
|---|---|
| YouTube URL in `©cmt` (durable provenance key) | 193 |
| artist tag = `X - Topic` (YouTube's own canonical artist channel) | 30 |
| artist tag = `XVEVO` | 42 |
| artist tag = some other channel (sometimes right: `Heart`, `Ashnikko`; often junk: `JediNg135`, `SyrebralVibes`) | 130 |
| artist tag blank | 15 |
| title parses as `ARTIST - TITLE` | 135 |
| channel agrees with the dash-parsed artist | 41 |
| **neither Topic/VEVO nor dash-parseable → needs external ID** | **52** |

13 `.ogg` (Opus) files carry **no tags whatsoever** — filename is the only
source. Filenames are mangled by the ripper's path-safe substitution:
`?`→`¿`, `/`→`⁄`, `|`→`¦`, `:`→`_`, plus a `(128kbit_AAC)` / `(152kbit_Opus)`
suffix on every file.

Not all of it is music: Lex Fridman podcast episodes, "ADHD focus music",
Netflix promo clips. Force-fitting those into artist/title is wrong.

## The precedence rule the merge evidence forces

The 3 verified work-level merges from #9265 all pair rips whose **channel tags
differ or are wrong**, and whose **dash-parsed title artist is identical**:

| merge | channels | dash-parsed artist |
|---|---|---|
| `bury a friend` (Lyrics / Official) | `SyrebralVibes` vs `BillieEilishVEVO` | `Billie Eilish` (both) |
| `We Know The Way` (+ From "Moana") | `DisneyMusicVEVO` both — VEVO-derived artist would be *"DisneyMusic"*, wrong | `Lin-Manuel Miranda, Opetaia Foa'i` (both) |
| `Concrete Wall (Pumpkin Remix)` (+ `[Free]`) | `LavelaL` vs `Proximity Chill` | `Zee Avi` (both) |

**Therefore: dash-parsed title artist must outrank the channel tag.** If the
channel tag won, two of the three merges break outright and the third gets a
wrong artist. This is not a style preference; it is what the data says.

Side effect to watch: the Concrete Wall pair is 293.2s / 293.3s. Once `[Free]`
is stripped as junk both titles normalize identically, so they will newly merge
at **recording** level too (they are literal duplicate rips, so that is correct
— but it changes taste-aggregation topology and needs a test).

## The real orphaning hazard — not retagging

`recording_id` / `work_id` are **derived fresh on every call** and never
persisted (`identity.py: build_identity_map`). PlayStat / TrackRating / Favorite
all bind to `track_id`. So retagging cannot orphan anything by itself.

The actual landmine is the scanner's update path, `_process_album_folder`:

    for t in list(existing.tracks): db.delete(t)   # then re-insert

Proven in an in-memory harness:

- **PlayStat rows are destroyed** by the ORM `delete-orphan` cascade.
- **TrackRating rows survive but detach** — no ORM relationship, and no
  `PRAGMA foreign_keys=ON` anywhere in `database.py`, so DDL-level
  `ON DELETE CASCADE` never fires.
- `tracks.id` is a plain `INTEGER PRIMARY KEY` (no `AUTOINCREMENT`), so SQLite
  **reuses rowids**. A detached rating can silently reattach to a *different*
  song on re-insert. That is worse than losing it.

This fires whenever the folder hash changes — i.e. the moment Todd drops one
new file into `q:\music`, all 207 tracks are deleted and re-inserted. A DB-only
repair is therefore **not durable**, independent of tag quality.

## Consequence for the design

Repairs go in a `track_tag_repairs` overlay table keyed by `file_path` (with
`source_url` from `©cmt` as a secondary key), applied by the scanner at insert.
Non-destructive, reversible, survives the delete/re-insert path, and keeps the
channel tag intact as evidence rather than overwriting Todd's originals.

## Tool availability

This session has **no `rfl_*` MCP tools** (only pantheon-orch, gmail, gcal,
gdrive, playwright). Proposed instead of building a blind external-ID client:
deterministic ladder server-side for the ~165 resolvable tracks, and export the
~52 residual as JSON (title, channel, YouTube URL, duration) for Jarvis's RFL
session to identify.

---

# Build + dry run (post-PROCEED #3038)

Phases 1-3 and 5 built; dry run produced. **No live write** — the report was
generated against a COPY of `server/audiplex.db`, so the live catalog and its
schema are still untouched pending Jarvis's ack.

## Files

- `server/audiplex/tag_repair.py` — the ladder (pure functions, no DB)
- `server/audiplex/tag_repair_report.py` — read-only dry run + RFL export
- `server/audiplex/models.py` — `TrackTagRepair` overlay, keyed by file_path
- `server/audiplex/database.py` — `PRAGMA foreign_keys=ON`, overlay migration
- `server/audiplex/scanners/music.py` — update-in-place, per-track artists
- `server/audiplex/scanner.py` — artist GC now spares artists that hold tracks
- `server/audiplex/manage.py` — `tag-repair-dryrun` command
- tests: `test_tag_repair.py`, `test_scanner_music_repair.py`,
  `test_identity_repair_survival.py`

**409 tests pass** (345 before, 64 new).

## Dry-run result over the live 217

| | |
|---|---|
| high (auto-applicable) | 184 |
| low (held) | 29 |
| unresolved (held) | 4 |
| artist would change | 184 |
| title would change | 153 |
| distinct artists after | 137 (from 2) |

## Identity impact — measured, not asserted

|  | work merges | recording merges |
|---|---|---|
| before | 3 | 0 |
| after | 4 | 1 |

All three #9265 merges survive. The new work merge is `Word Up (Relaid Audio)`
+ `Word Up!`, both Cameo — the same song, correctly pooled for cooldown while
staying separate recordings for rating. The new recording merge is the Concrete
Wall pair, exactly as predicted and signed off in plan-back #9272.

## False positives the dry run caught (fixed, each with a test)

1. `4 Non Blondes` -> `Non Blondes`. The leading-track-number strip ate a digit
   that was part of the name; it now requires punctuation (`01. `, `03 - `).
2. `Ode to Josephine By Tumbledown House` as an artist. When the channel sits
   INSIDE the parsed artist rather than matching it, the dash split in the
   wrong place — downgraded to review.
3. `Cups Pitch Perfect's When I'm Gone` as an artist. A credit longer than five
   words with no channel corroboration is a sentence, not a performer —
   downgraded to review.
4. `I LOVE PARIS (Cole Porter)` as an artist. When the channel matches the half
   AFTER the dash and not the half before it, the title is written
   title-dash-artist and the halves are swapped. Two signals, so it stays high.

## Known residuals (not fixed — would exceed the approved plan)

- **Album-level artist consensus.** Two Marillion tracks carry no artist tag of
  their own, so they stay "Various Artists" while their eight tagged siblings
  become "Marillion". Every tagger resolves this by consensus across the album
  folder; ~10 lines, but outside plan-back #9272. Flagged for a ruling.
- **Non-music.** Lex Fridman, ADHD focus music, Netflix clips land in the review
  bucket per the Q1 ruling — nothing invented. Classifying them is #954.
- `Bea Miller / "Playground | Arcane League of Legends"` keeps a trailing pipe
  segment. Cosmetic; stripping it generally would eat real titles.

---

# LIVE WRITE APPLIED (authorized by #3040)

Ran `python -m audiplex.manage tag-repair-apply --yes` against
`server/audiplex.db`. Backup first: `audiplex.db.bak-20260818-tagrepair`.
Rehearsed twice on copies before touching the real thing, including a second
run to prove idempotence.

```
before: {'tracks': 217, 'artists': 2,   'play_stats': 7, 'ratings': 0}
        dropping empty artist ''
after : {'tracks': 217, 'artists': 138, 'play_stats': 7, 'ratings': 0}
repairs applied: 186   held for review: 31
```

186 = the 184 parser high-confidence rows plus the 2 Marillion tracks that
inherited their siblings' artist through the new folder consensus. Held drops
33 -> 31 for the same reason.

Verified on the live DB afterwards: 217 tracks (same rows, same ids), 138
artists, **zero blank-artist rows**, play statistics still 7, and the identity
map showing 4 work merges + 1 recording merge — the three from #9265, the new
Cameo "Word Up" pair, and the Concrete Wall recording merge. Exactly the
dry-run prediction, no drift.

The apply runs the production scanner rather than a bespoke migration, so the
repair path and the ingest path cannot diverge. It is idempotent: a second run
reports the same counts and writes nothing.

## Server restarted

The running server (PID 35236) was still on the OLD code, which meant its scan
endpoint could still delete-and-reinsert every track and take the play
statistics with it. Killed and relaunched via `launch-hidden.vbs`; now PID
43568, `/docs` returns 200. That hazard is closed rather than merely fixed in
source.

## Follow-ups filed, not done

- #954 — `kind` classification for non-music. Exhibit: the "ADHD Music" artist
  on the Greenred focus track, a dash-parse artifact Jarvis accepted on the
  record as no-worse-than-blank.
- #955 — the same delete/re-insert hazard in the book and meditation scanners.
- The 31 held rows await the RFL identification pass; verdicts import through
  the same overlay with `source='rfl'`.
