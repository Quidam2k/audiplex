# #3249 — Audiplex phone fixes for the coordinated phone bundle

Staged, NOT built (the gradle build auto-bumps versionCode; no bump from this worker).

## Already in source, never built (the installed APK predates it)
From e194eea (`feat(#2843,#2842): phone DJ acks tell the truth + replace_upcoming`):
- `replace_upcoming` handled. Today the phone acks `unknown_type`, so dj_mix can only append.
- Idle heartbeat on the now-playing report (`shouldReport`, DjAckHonestyTest). Today a
  player stuck in ERROR/IDLE never reports, because the playing:trackId:index key never
  changes. Its snapshot stayed 4+ min stale on 2026-09-28.
- Truthful acks. Today play_now acks ok even when the stream then 404s.

## New, needs writing (from 2026-09-28 live evidence)
- `DjCommandClient.resolveTracks` does one `api.getTrack(id)` per id, in sequence, and the
  command loop stops long-polling meanwhile. A ~990-track `queue` kept the phone dark
  ~3 min twice (13:17:32→13:20:07, 13:37:22→13:40:20). The server's link-history logged
  both as `gap`, and personas read it as "NO PLAYER CONNECTED" or a cellular handoff.
  Fix: resolve concurrently (bounded, e.g. 8 at a time) or add a batch
  `GET /api/music/tracks?ids=` and use it. Also keep polling while a command resolves.
  Server-side mitigation already shipped: the MCP sends lists in 40-track commands (dd64d79).
- Focus loss while DJ music plays (AUDIO_FOCUS_LOSS at 13:20:55, 13:21:50) belongs to #2845/#991.

## Verify after install (emulator first, never Todd's phone mid-listen)
dj_mix on a loaded queue → `replace_upcoming` acks ok. A 200-track dj_queue → no link-history gap.
A track whose file is missing → the ack or client-log says so, and dj_now_playing shows it.
