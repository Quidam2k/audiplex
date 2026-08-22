# #3161 — Add Todd's priority music root (E:\Stacked Deck\Music\"Todd Walker - music")

Team: worker-audiplex-library-roots--20260822-183108-02c9
Status: PLAN-BACK SUBMITTED, awaiting PROCEED. Todd is mid-ride — do not restart server without approval.

## Facts (verified on disk / in code)
- Folder: 364 files = 277 .m4a + 87 .opus, ALL in one flat dir (no subfolders).
- 34 of the 87 .opus are UNIQUE songs (no .m4a twin); 53 opus are cross-codec dupes.
- server/audiplex/scanners/music.py AUDIO_EXTENSIONS lacks ".opus" -> 87 files silently skipped (not even counted).
- server/audiplex/utils/streaming.py EXT_MIME lacks ".opus" -> would serve octet-stream. (".ogg"=audio/ogg IS present.)
- Flat folder => one "Various Artists" album titled "Todd Walker - music", genre None; tag_repair ladder attributes per-track (untagged-pile risk).
- compute_file_hash = size+mtime (no content read); scan is I/O-light & fast. #842 = no destructive sweeps for reachable roots.
- Server LIVE on :8100. jwt_secret in server/config.yaml. Admin user = id 1 'admin'. PUT /api/music/roots does cache_clear in-process (no restart).
- Existing music roots to PRESERVE in the PUT body: q:\music ; H:\Stacked Deck\Music\Artists & Albums\Mixed\Ren Faire. New: E:\Stacked Deck\Music\Todd Walker - music.
- DB now: 1226 tracks / 56 albums / 599 artists.

## Plan
- Phase 1 (NOW, no restart): mint admin JWT, PUT /api/music/roots with 3 roots -> live scan ingests 277 .m4a. Report counts + tagging quality.
- Phase 2 (DEFERRED until ride done / approved): add ".opus" to scanner AUDIO_EXTENSIONS + streaming EXT_MIME (audio/ogg), add test, commit, restart server, rescan -> +87 opus (34 unique). RESTART = the disruptive step.

## Individual folder note (option only, NOT an action)
- E:\Stacked Deck\Music\Individual: ~2,218 audio (1904 mp3 +245 m4a +68 ogg +1 wav) + 1082 jpg/junk. Hybrid: 9 subdirs + 365 loose root files.
- Feasible with NO code change (opus here are .ogg, already supported). Advise doing AFTER opus lands; expect large Various-Artists pile needing tag review; dupes with Todd-Walker-music folder.

## EXECUTION LOG (post-PROCEED #3162)
- A1: reported exact PUT payload + endpoint to Jarvis (event #9711). Jarvis mints JWT + runs PUT (workers don't mint creds). AWAITING his scan result.
- A2: DONE. .opus added to scanner AUDIO_EXTENSIONS + streaming EXT_MIME(audio/ogg) + 2 tests. Full affected suites 62 passed. Commit fb62838 pushed to origin/master. deploy_pending #66 filed (kind=flag/owner=jarvis; enum lacks restart:audiplex-server — restart is Jarvis's call, then rescan).
- Tag-quality forecast (computed from real files via tag_repair.propose, = what the scan will write):
  - Phase1 277 .m4a: 0 unreadable, 244 APPLIED (real per-track artist), 33 PENDING_REVIEW (Various-Artists fallback worklist). Consensus None.
  - Full 364: 331 APPLIED, 33 PENDING, 0 unreadable.
- STILL OPEN: Jarvis's Phase-1 PUT scan result to confirm DB matches (album "Todd Walker - music", ~277 tracks). Do NOT complete #3161 until then.

## PHASE-1 EXECUTED (A1-amended #3163) — DONE
- config.yaml: added music root E:\Stacked Deck\Music\Todd Walker - music (gitignored; persists for Phase-2 restart).
- Ingest: ran scan_music over ONLY the new root vs live DB, 0.34s lock-hold, added=1 album, 0 errors.
- DEVIATION+FIX: the ingest script imported the already-patched scanner, so it pulled all 364 (incl 87 .opus). Live server (un-restarted) can't stream .opus with correct MIME yet, so I rolled the 87 .opus back to restore the dual-signed 277-m4a Phase-1 state (tag_repair verdicts kept). Opus returns at Phase-2 restart+rescan.
- FINAL DB: album 4158 "Todd Walker - music", 277 m4a tracks. 244 applied (real per-track artist) / 33 pending_review (Various-Artists fallback worklist). 0 unreadable.
- Live playback: streams by DB path (Jarvis-verified), so the 277 are playable NOW without restart. Cached-roots staleness: live server's own narrow rescan won't see this folder until the Phase-2 restart (noted for deploy_pending #66).
