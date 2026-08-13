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

## Deliberately out of scope
Retagging the 206 untagged files so artists/albums/genres populate properly is
the durable fix, but needs a metadata pass plus a live rescan mid-test.
Recommended as a separate item; browse + search meets the acceptance bar
without it.
