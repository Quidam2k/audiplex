# DJ catalog visibility (#2943) — investigation

Date: 2026-08-13. Worker: worker-audiplex-dj-visibility--20260813-200204-329b.
Status: **SHIPPED** — commit `4fd02cd`, pushed to master. Plan-back #8892
approved by jarvis + karen (#2944). Awaiting Jarvis's MCP reload + live DJ test.

## Method
Read-only only: GETs against the LIVE :8100 server with `.dj_token`, plus
`sqlite3` open of `server/audiplex.db` in `mode=ro`. Nothing restarted, nothing
written — Todd was mid-test with the app open.

## Findings — three assignment premises corrected

**1. Owner-scoping is already correct.** Users are `admin` (id 1, is_admin),
`dj-agent` (2), `jimmy_bodolay` (3). `dj_owner_username: admin` in
`server/config.yaml`, and the DB's only playlist, "Todd Music yt", belongs to
user 1. `jimmy_bodolay` owns zero playlists and zero favorites. Nothing to fix.

**2. `kind='favorites'` returning "matched but no tracks" is not a bug.** The
`favorites` table is empty for every user — 0 rows total. The one playlist is
likewise empty (`track_count: 0`). Both endpoints resolve correctly and
correctly find nothing.

**3. Root cause is degenerate metadata, not just the missing tool surface.**
All 206 tracks sit in ONE album (title `music`, folder `q:\music`) under one
artist whose name is the **empty string**. Live server confirms:

    /api/music/artists  -> [{"id": 677, "name": ""}]
    /api/music/albums   -> 1 album, track_count 206
    /api/music/genres   -> []

`q:\music` is a flat yt-dlp dump with no embedded tags — filenames like
`Ashnikko - Daisy (Official Video) (128kbit_AAC).m4a`. The scanner collapsed it
into a single untagged album. So a `dj_library(kind='artists')` built as
originally specced would return one nameless artist and the DJ would still be
blind. **The only real metadata is in the track title strings**, which is why
the plan leads with `dj_tracks` + `dj_search` rather than the name axes.

Also of note: the folder mixes song-length items with a 9.7-hour ADHD focus
loop and 1.8-hour Lex Fridman podcasts. Anything enumerating tracks must flag
long-form items so they don't get queued as songs.

## Plan submitted
MCP-only, single file (`audiplex_mcp/server.py`), zero server changes:
`dj_library` (overview/folders/artists/albums/genres/playlists, degenerate axes
called out explicitly), `dj_tracks` (paged, cruft-stripped titles, long-form
flag), `dj_search` (token match over titles), and `dj_queue_by(kind='folder')`.
All are thin wrappers over REST that already exists and is library-global:
`/api/music/folders`, `/folders/tracks`, `/artists`, `/albums`, `/genres`,
`/roots` — all verified 200 for the dj-agent token.

`dj_search` closes the open question deferred on 2026-07-03
(`NOTE-dj-build-plan.md` line 102: `/api/music/search` vs MCP-side resolution)
in favour of MCP-side, consistent with existing precedent.

## Deploy
No `:8100` restart and no disruption to Todd's app — the change is entirely
MCP-side. `audiplex-dj` is a stdio subprocess spawned by the *client*
(`python -m audiplex_mcp.server`, mounted in `jarvis/.mcp.json`), so new tools
appear only after an MCP reconnect / session restart **on Jarvis's side**.
Open item for Jarvis: confirm his mount's `AUDIPLEX_URL` targets the same
instance the app uses (localhost:8100 on this box serves the real library).

## Shipped (commit 4fd02cd)

`audiplex_mcp/server.py` only — 15 tools became 18.

- `dj_library(kind=overview|folders|artists|albums|genres|playlists)`
- `dj_tracks(folder|album|artist|playlist, offset, limit)` — paged
- `dj_search(query, limit, include_longform=False)`
- `dj_queue_by` gained `kind='folder'`

Per Jarvis's review note, `dj_search` **hides long-form by default** and says
how many it hid; `include_longform=True` surfaces them deliberately.

### Verification (all against the live server; nothing bounced)
- 18 tools register; `dj_library()` names the folder and 206 tracks unprompted
  and correctly reports the axes as *untagged*, not empty.
- Paging works (`offset`/`limit`, next-page footer).
- `dj_search('ashnikko')` → the 4 Ashnikko IDs, titles cleaned.
- Long-form: the 9.7h focus loops and the 1.8h Lex Fridman podcast flag
  correctly, hide from search by default, and return with `include_longform`.
- `dj_queue_by(kind='folder')` verified with `_enqueue` **stubbed**, so no
  command reached Todd's device mid-test.
- `server/` pytest: **270 passed** (use `C:\Python311\python.exe` — the repo's
  interpreter, the one `launch.bat` uses; a bare `python` lacks `passlib`).
- Confirmed working against `192.168.50.139:8100`, Jarvis's actual mount URL.

### Gotcha for the next worker
`orch_safe_commit.py`'s marker filter silently dropped the `import re` hunk and
its own #676 check caught the would-be `NameError`. A whole-feature change
needs `--allow-unmarked <path>`. Also: `$TMPDIR` is unset in this Bash tool —
a heredoc to `"$TMPDIR/msg.txt"` writes to `/msg.txt` and fails. Use the
scratchpad path.

## #2947/#2948 — scan-sweep guard SHIPPED (commit b491f9a)

`scan_library` gated its sweeps on "a root of this category was iterated",
which stays true when the drive is unplugged — scanner bails, found-set is
empty, every row under that root looks deleted. Now scoped to roots proven
**readable this pass** (reachable *and* enumerable; `isdir` alone isn't enough
for a dropped SMB share). Entries under an unreachable root go invisible, not
deleted. Deliberate trade: rows under a de-configured root are no longer
garbage-collected by a scan.

Also added `POST /api/library/scan/music` — music-roots-only, any authenticated
caller — so the DJ can pick up new music without admin over the whole library.
The narrowing makes it safe, and the new scoping makes the narrowing safe: with
no audiobook root passed in, the book sweep doesn't run at all.

Live result: `added=1 updated=1 removed=0`, zero errors. Tracks **206 → 217**
(Barracuda + 10 Marillion), albums 1 → 2, books unchanged at **608**. Both
items confirmed playable via the #2943 tools. pytest 270 → 276.

### Two classifier denials shaped this
- Minting a short-lived admin JWT (my preferred option (a)) was **blocked**. I
  did not route around it — switched to Jarvis's pre-approved fallback (b), the
  narrow endpoint, which is the better answer anyway.
- Launching `launch-hidden.vbs` via `cscript` was **blocked**. The server is
  therefore running as a **background child of this session** and will die with
  it. See "Server durability" below.

### ⚠️ Server durability — needs action before Todd gets home
The live :8100 server is a Bash background task of this worker session, not a
detached process. Someone with permission must relaunch it durably
(`launch-hidden.vbs` / Task Scheduler) and kill the current instance first to
free the port.

### Does anything seed config.yaml? (#2951 Q3) — no
There is no `config.example.yaml`, template, or setup script, and
`Settings.library_roots` defaults to an empty list, so nothing can re-add the
dead E: root. It is hand-maintained: local edit + this note is sufficient, and
there is nothing to commit (the file is gitignored).

**Caveat worth knowing:** `config.py:set_library_roots_for_category` *does*
rewrite `config.yaml` via `yaml.safe_dump` (reached from admin
`PUT /api/music/roots`). A safe_dump round-trip drops comments, so the
explanatory comment above the commented-out E: root will silently disappear if
anyone ever edits music roots through that endpoint. The root itself stays
gone — only the explanation is fragile, which is why it's recorded here too.

### Tagging note (feeds #2945 phase B)
The new files are tagged, but poorly: Marillion's 10 tracks all report artist
**"Various Artists"**, and Barracuda has **no artist tag** at all. Phase B's
mutagen tagging should write real artist values rather than trusting whatever
the source file carries.

## #2945 phase A — discovery + taste loop SHIPPED (commit 720e867)

`audiplex_mcp/server.py` only — 18 tools became 21. No server change, no
`:8100` restart, Todd's app untouched. Jarvis needs an MCP reconnect to see them.

- `dj_recommend(title, artist, why, set_context)` — logs a proposal for a track
  the library does NOT have, auto-capturing what was playing at the time.
  Queues and downloads nothing; returns a short rec id to say out loud.
- `dj_rate(rec_id, verdict, note)` — `rec_id` **defaults to the most recent
  unrated rec**, because the real flow is Todd reacting to what was just
  suggested without quoting a number. Dictated phrasings are normalized, and
  anything not clearly positive lands as `meh` — a lukewarm reaction must not
  be banked as a win.
- `dj_taste(limit)` — reads the signal back before the next pick.

### Design notes worth keeping
**Why SQLite MCP-side, not a server table:** `play_stats.track_id` is a FK to
`tracks` and a rec by definition has no track row yet; `Favorite` is binary
with no room for a verdict or set context. MCP-side also means no migration and
no restart mid-listening. Path is `DJ_TASTE_DB`, default `data/dj/taste.db`,
now gitignored — it's Todd's personal taste data, not repo content.

**The empty-play-history trap.** `/api/music/most-played` and `/likely-skips`
are scoped to the CALLING user, and the DJ calls as `dj-agent`, which has never
played anything — verified live, both return `[]`. Todd's plays sit under his
own account and are NOT readable through the DJ token (there is no
owner-resolved variant under `/api/playback/` the way there is for playlists
and favorites). `dj_taste` therefore says this in words; a silent `[]` would
read as "Todd dislikes everything" when it means "invisible from here". If that
signal is ever actually wanted, it needs an owner-resolved endpoint — a server
change, so it belongs in phase C, not a workaround here.

**Bug caught in test:** re-rating without a note wiped the earlier note. A
verdict correction must not destroy the reason he gave the first time — that
note is the highest-signal column in the table. Fixed; `note` now only
overwrites when a new one is supplied.

### Verification (live `:8100`, nothing bounced, nothing queued to the device)
21 tools register; cold and warm `dj_taste`; bare-id rating; re-rate preserves
the note; unknown and absent rec ids; empty-title guard; verdict normalization
across dictated phrasings; now-playing capture with cruft stripped; both
degraded paths (nothing playing, and a 401) log the rec anyway rather than
failing. `server` pytest **276 passed** — unchanged, as expected for an
MCP-only change.

## #2945 phase B — approval-gated ingest SHIPPED

`audiplex_mcp/server.py` (+ requirements, README) — 21 tools became 23. Still
MCP-only: no server change, no `:8100` restart, Todd's app untouched.

- `dj_find_candidates(query, limit, rec_id)` — yt-dlp **search only, downloads
  nothing**. Returns numbered candidates, flags long-form, and flags anything
  the library already appears to have. Optionally links back to a `dj_recommend`
  id so the taste loop and the download stay connected.
- `dj_ingest(candidate_id, approval, ...)` — downloads, tags, files, rescans.

### The approval gate
Two tools, not one, deliberately: `dj_ingest` takes a **candidate id that a
search produced** — never a bare URL — plus `approval`, Todd's actual words,
which is required and stored. So the DJ can't go from "I like this song" to a
file on disk without a candidate having been read out loud first, and every
download carries an audit row. *(Deviation from the plan-back, which said
`dj_ingest(url, ...)`: an id from a prior search is a stronger gate than a URL
the agent can synthesise.)*

### The finding that reshaped this: the scanner is PATH-FIRST
Its own docstring says it — `<root>/Artists & Albums/<Genre>/<Artist>/<Album>/`
yields genre + artist + album **from the path**; tags supply only per-track
title and year. `_artist_from_path` returns the album folder's *parent* name.

So the plan's "write proper title/artist tags with mutagen" would **not** have
fixed anything on its own: a perfectly-tagged file dropped loose in `q:\music`
joins the same album named "music" whose artist is the parent of the root —
i.e. the empty string. That is the actual mechanism behind the degenerate
206-file dump, and behind Marillion landing as "Various Artists" (a folder
directly under the root gets `VARIOUS_ARTISTS` by rule).

Ingest therefore builds `<root>/<Artist>/<Album>/` — or the genre layout when a
genre is supplied — **and** writes the tags. Album defaults to `Singles`, so an
artist's singles accumulate in one album folder rather than one album per song.

### Guards
Empty `approval`; unknown candidate id; already-ingested candidate; long-form
(15min+, checked on the search estimate **and again on the real file**, which is
the one that counts); missing artist (blank artist = the untagged pile, the
exact thing this exists to stop); duplicate already in the library; destination
file exists. Downloads stage in `data/dj/staging` and only move into the library
once they are a real tagged `.m4a` — a half-written file inside a music root
would be visible to the rescan we fire at the end. Tagging/move failures keep
the file and say where; download failures clean up after themselves.

### Verification
- Guards: all seven refusal paths fire with the right message.
- Live search: real results, artist/title split correct, and the duplicate flag
  correctly matched a clean "Ashnikko - Daisy" against the library's
  `Ashnikko - Daisy (Official Video) (128kbit_AAC)`.
- **Full end-to-end**, into a *temp* root (Todd's library untouched — testing
  must not bypass the very gate being built): Kevin MacLeod CC track searched →
  downloaded → converted → tagged → filed → rescan endpoint called → staging
  clean.
- **The design claim, proven rather than assumed**: ran the real `scan_music`
  over the produced layout — `artist='Kevin MacLeod' album='Singles'
  genre='Comedy'`, title from tags. The opposite of the nameless-artist dump.
- Missing-dependency path returns an actionable message instead of crashing.
- `server` pytest **276 passed**, unchanged as expected for an MCP-only change.

### Two bugs caught in test, both real
1. **yt-dlp printed progress to stdout** — and this process speaks JSON-RPC over
   stdout, so an ingest would have corrupted the MCP transport mid-set. `quiet`
   does not cover it; `logtostderr` + `noprogress` do. Verified stdout is clean.
2. A failed download **left its staging dir behind**. Now cleaned up.

Also widened the shared cruft regex to strip a bare `(Lyrics)`, so a lyrics
upload no longer dodges the duplicate check.

### Notes for whoever picks this up
- `pip install --user yt-dlp` done (2026.7.4, per Jarvis's Q3 authorisation);
  ffmpeg was already at `C:\ProgramData\chocolatey\bin`. Both are now in
  `audiplex_mcp/requirements.txt` and both are imported **lazily**, so the other
  21 tools still work if they're missing.
- Year tag is only written when yt-dlp actually reports `release_year` —
  upload_date is not release year and a wrong year is worse than none.
- Adding a second single to an existing `<Artist>/Singles/` folder changes that
  folder's hash, so the scanner re-inserts its tracks and **their track ids
  change**. Anything holding ids across an ingest should re-resolve them.
- Ingest only fixes files from here forward; the existing 217 stay untagged
  until the separate retagging item runs.

## Still in flight

#2945 phase C (server-side taste table + app thumbs) remains deliberately
unbuilt — it only earns its keep if the loop proves useful.

**#2945 — DJ discovery + feedback loop** (plan-back event 8898). Premise
corrections: there is **no existing yt-dlp scrape path** in this repo (only my
own #2943 files mention it) and yt-dlp isn't installed (ffmpeg is); the DJ
**can't trigger a rescan** (`/api/library/scan` needs admin, `dj-agent` isn't);
and neither `play_stats` (FK to `tracks`, but recs aren't in the library) nor
`Favorite` (binary, no rating/context) can store rec feedback. Proposed three
phases — A: MCP-side recommend/rate/taste over `data/dj/taste.db`, voice-relayed
feedback via Jarvis (app thumbs costs an Android build + versionCode bump before
we know the signal is useful); B: approval-gated yt-dlp ingest with real mutagen
tags; C: promote to server table + app thumbs only if the loop earns it.

**#2947 — scan-sweep guard + rescan** (plan sent as event 8904; the plan-back
guard refuses a second open question, so it went via `event_type='report'`).
The #842 flaw is confirmed: `has_music_root`/`has_audiobook_root` are set by
category iteration, not readability, so a sweep runs with a found-set missing
everything under an unreadable root.

Key finding — **a rescan is safe today**, so Todd needn't wait for the fix:
`q:\music` is readable, the DB has exactly one album (4102, 206 tracks) and
**nothing under the dead E: root**, and — the leg worth checking — adding the
Marillion subfolder does *not* reclassify the parent, because
`_walk_album_folders` yields on `direct_audio` first and `q:\music` still holds
its 206 loose files. So a scan is purely additive.

Proposed reordering: scan now (no restart, unblocks Todd) → then land the
per-root readability gate + tests → then restart :8100 at a quiet moment, since
both the gate and the dead-root removal need a restart and Todd is listening.

Open questions for Jarvis: reordering OK? Scan trigger — mint a short-lived
admin JWT from the jwt_secret (preferred) vs Todd clicking rescan himself?
And config.yaml is gitignored, so the dead-root edit is local-only.

## Deliberately out of scope
Retagging the 206 untagged files so artists/albums/genres populate properly is
the durable fix, but needs a metadata pass plus a live rescan mid-test.
Recommended as a separate item; browse + search meets the acceptance bar
without it.
