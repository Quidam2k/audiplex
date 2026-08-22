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
