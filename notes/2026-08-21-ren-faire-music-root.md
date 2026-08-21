# Ren Faire archive added as a scoped music library root (2026-08-21)

**Why a foreign H:\ root exists in the library.** Pantheon assignment #3113/#3114
(suggestion #979), Todd's explicit design: *"expose the folders she would be
interested in to Audiplex and she just uses Audiplex as her tools — we don't have
to build her anything separate."* Karen (and the other DJ personas) needed the Ren
Faire mix archive visible through the existing DJ tools (dj_library / dj_tracks /
dj_search) instead of a bespoke per-agent file allowlist.

## What changed
Added ONE music `library_root` to `server/config.yaml`:
```yaml
- category: music
  path: H:/Stacked Deck/Music/Artists & Albums/Mixed/Ren Faire
```
alongside the existing `q:\music`. Applied via
`config.set_library_roots_for_category("music", ["q:\\music", <Ren Faire>])`
(atomic temp+rename, preserves the JWT secret and non-music roots), then a
music-scoped `scan_library` populated the DB.

## SCOPE GUARD — read before touching this
This is the Ren Faire **subtree only** (~1,006 audio / 1,714 total files:
Prospectives, On Deck Circle, Northern Ren Faire '05, Southern Ren Faire
'01–'08). It is deliberately **NOT** the full `H:\Stacked Deck` tree — that
full-terabyte index is Pantheon #956, sequenced separately with its own
scanner-hardening prerequisites. Do not widen this root to the parent folder.

## Why it was safe to add live (no server restart, no admin token)
- Browsing reads DB rows (Track/Album/Artist), not config — so a scan run by any
  process that writes the tracks into `audiplex.db` makes them visible immediately.
  The add + scan ran OUT-OF-PROCESS (a one-shot using the server's own modules),
  because `.dj_token` is the non-admin dj-agent and the live server's
  `get_settings()` is `@lru_cache`d (an external YAML edit is not seen by the
  running uvicorn process until it reloads).
- #3037 in-place upsert verified: ratings/play-stats/favorites/playlist entries all
  FK `tracks.id ON DELETE CASCADE`, `tracks.file_path` is UNIQUE, and `_sync_tracks`
  (scanners/music.py) matches by file_path and updates IN PLACE (ids preserved).
  A new root = a new album (folder_path not in DB) = all-new tracks → existing
  roots (`q:\music`, meditation, audiobooks) and their ratings are untouched.
- Stale-cache safety: if the live server later runs `/scan/music` with its cached
  roots (only `q:\music`), it will NOT sweep the Ren Faire albums — they are not
  under `q:\music`, so they fall outside `readable_album_roots` (the #842 sweep
  scope). The config.yaml entry means a server restart picks the root up properly.

Coordinated with the concurrent audio-mixer worker (on the #998 companion voice
bug, phone Kotlin — no config.yaml/roots overlap; it gave the all-clear before the
change).

Reversible: remove the root (`set_library_roots_for_category("music", ["q:\\music"])`)
and rescan.

Cross-ref: Pantheon `notes/979-karen-pgate-package.md` (full package, incl. the
Part-2 gemini-wrapper remote-answer plumbing shipped as Pantheon SHA 1888780).
