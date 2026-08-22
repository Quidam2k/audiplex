# #3124 — Audiobook library re-point: Athena share → P:\Books

**Date:** 2026-08-21 (~6:00 PM PDT)
**Context:** Follow-on to #1001 (the play-stall / 503 hardening). The
`\\Athena.local\Users\athen\Libation\Books` SMB share broke "a while back",
404/503-ing all 598 audiobooks. Todd copied his Libation/Audible backup to
`P:\Books` (byte-for-byte, same relative structure). This task re-points the
library at the new location so the books stream again — acceptance = book 40
"Anathem" streams (he's mid-book).

## What was done (data-plane only — no source changes)

1. **Copy verified complete + quiesced** before touching anything: 3 samples
   (5:50 / 5:53 / 5:55 PM) all showed 598 `.m4b` under `P:\Books` (== DB book
   count) and Anathem = 927,985,549 bytes == the DB's recorded `file_size`.
2. **DB backup** (WAL-safe `sqlite3.backup()`):
   `server/audiplex.db.bak-3124-1787360149` (608 books, 9 positions). Rollback
   point if needed. (gitignored — lives beside the DB.)
3. **DB prefix-swap** in one transaction: for the 598 books under the old
   prefix, `file_path` and `cue_path` had `\\Athena.local\Users\athen\Libation\Books`
   → `P:\Books` (backslash form, exact match to a future scan's `str(Path)`).
   Result: 0 books still point at Athena; all 598 new paths `os.path.exists()`.
4. **Config root swap**: `set_library_roots_for_category('audiobook_clean',
   ['P:/Books'])` in `server/config.yaml`. Meditation + music roots untouched.

## Position preservation (#955 verdict)

`_scan_libation` matches Book rows by `file_path` and **updates in place**
(book.id preserved); it only delete/reinserts *Chapters* (which hold no
positions). The #955 delete/reinsert hazard is the **music track** scanner, not
the Book row — so it does **not** apply here. The prefix-swap keeps every
book.id, and `playback_positions` keys on `book_id`. Verified: 9 positions
identical byte-for-byte before and after, **including Anathem's own (pos_id 9)** —
Todd's saved spot survives.

## Acceptance — PASS

`GET http://localhost:8100/api/stream/40` with `Range: bytes=0-1023` →
**HTTP 206**, 1024 bytes, first bytes `\x00\x00\x00 ftypisom` (valid M4B/MP4
`ftyp` box). Real audiobook data served from the live server.

## Note on the running server's config cache

`get_settings()` is `lru_cache`d **per process**. The config-file edit persists
to disk and cleared the cache in the *editing* process, but the live :8100
server keeps its old cached `library_roots` until it next reloads/restarts.
This does **not** affect streaming: `stream_audio` serves the file whenever
`os.path.exists(book.file_path)` is true and only consults roots on the *missing*
path (the 503 branch). The one live gap until a restart: if `P:` itself drops,
those books would 404 rather than 503 (the offline-root helper doesn't yet know
`P:\Books` is a root in the running process). Self-resolves on next restart;
`scan_on_startup` is false so no surprise scan on that restart.

## FOLLOW-UP (deferred, approved by Jarvis + Karen) — off-hours full rescan

A full `scan_library` was **deliberately deferred**. The scanner runs
`compute_file_hash` on every file before the hash-skip, i.e. hundreds of GB of
reads across 598 books — that would hammer the disk during Todd's listening
evening for zero tonight-benefit (streaming reads `file_path` directly; the
config swap already prevents dup-insert and false-503 on future scans). Run it
in the next quiet window (tomorrow pre-noon): it will hash-skip all 598 (bytes
unchanged) and confirm metadata, and — via a restart or reload — the running
server picks up `P:\Books` as a known root for the 503 helper.
