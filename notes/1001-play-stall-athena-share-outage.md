# Pipeline #1001 — Audiplex play-stall diagnosis (2026-08-21)

**Symptom (Todd, 5:12 PM PDT):** track "loads into the Audiplex player but won't START"; app sluggish.

## Root cause (CONFIRMED, with log lines)

The item Todd pressed play on was the **audiobook "Anathem" (book id 40)**, not a
music track. Its file lives on the Athena SMB share, which is **currently
unreachable** from the Audiplex host (Solace), so the stream endpoint returns 404
and Media3 fails the source.

Client-log (`GET /api/playback/client-log`, in-memory ring buffer), incident window:

```
id 89  play_when_ready=true reason=USER_REQUEST playbackState=BUFFERING  (no trackId → a BOOK)
       at 1787357468.219  = 2026-08-22 00:11:08 UTC = 17:11:08 PDT
id 90  player_error "Source error" code=ERROR_CODE_IO_BAD_HTTP_STATUS
       cause=InvalidResponseCodeException "Response code: 404" bookId=40   17:11:11 PDT
id 91  (same) 17:11:28   id 92 (same) 17:11:46   id 93 (same) 17:11:55
```

Server path — `routers/streaming.py::stream_audio` (`GET /api/stream/{book_id}`):
```python
file_path = book.file_path
if not os.path.exists(file_path):
    raise HTTPException(status_code=404, detail="Audio file not found on disk")
```

DB (`server/audiplex.db`):
- book 40 = "Anathem", category audiobook_clean,
  file_path `\\Athena.local\Users\athen\Libation\Books\Anathem [B002V8KY4Q]\Anathem [...].m4b`
  → `os.path.exists` = **False**.
- **598 of 608 books** are on `\\Athena.local\Users\athen\Libation\Books`; **all 598 are MISSING** right now. The UNC root itself does not resolve.
- Athena host is UP (mDNS ping fe80::… replies 1ms) but the **SMB share is down** (`\\Athena.local\Users` → "No such file or directory"; Tailscale name → General failure).

## The pipeline's "prime suspect" (H: Ren Faire music) is EXONERATED

- H: scan finished in ~16 s at 16:55 PDT (added_at 23:55:26–23:55:42 UTC); every H: track has valid duration_seconds, file_size, album_id.
- H: files exist and read fast from the host (64 KB in ~2 ms). q: music also serves fine.
- Music tracks were never the failing item; the failure was an audiobook on the Athena share.

## Fix

**Primary (infra, Todd — not a code fix):** restore access to the audiobook share
`\\Athena.local\Users\athen\Libation\Books` (re-establish the SMB mount / bring the
share back), OR migrate the Libation audiobook library to a durable local path and
update `books.file_path`. Likely entangled with the Q: migration cutover.

**Secondary (server-side hardening, proposed — needs signoff):** when
`os.path.exists(file_path)` is False, distinguish "storage root offline" from
"file genuinely deleted." If the file's configured library ROOT is itself
unreachable, return **503** (retryable; Media3 will retry, and the app can show
"library storage offline") instead of **404** (which Media3 treats as a permanent
source error). This turns a mysterious permanent stall into a self-healing retry
once the share returns. Does NOT restore playback on its own — only restoring the
share does.

## Notes
- Read the client-log via the existing `.dj_token` service credential (read-only endpoint).
- Persisted client-exits/link-history end at 14:47 PDT (before incident) — the app did
  NOT crash/exit during the stall; it stalled on the 404, consistent with the above.
