# DJ catalog visibility (#2943) — investigation

Date: 2026-08-13. Worker: worker-audiplex-dj-visibility--20260813-200204-329b.
Status: plan-back submitted (event 8892, HIGH crit), holding for PROCEED. No code written.

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

## Deliberately out of scope
Retagging the 206 untagged files so artists/albums/genres populate properly is
the durable fix, but needs a metadata pass plus a live rescan mid-test.
Recommended as a separate item; browse + search meets the acceptance bar
without it.
