# DJ control (#429/#431/#439) — orientation & DELTA (assignment #2922, 2026-08-12)

Worker: worker-audiplex-dj-control--20260812-224422-00b7. Host: **Solace** (production box).
Status: plan-back #8824 APPROVED (jarvis + karen, PROCEED #2925). **Both milestones BUILT and GREEN.**
See "BUILD RESULTS" at the bottom for what shipped.

## Headline
**#429 is already 100% code-complete. There is no #429 build work left.**
The assignment's stated risk (rebuilding shipped work) was real — this is the finding.

## Governing rescope
#439 (2026-06-25, reaffirmed): app-unification **REJECTED**. Audiplex, the music player,
Radio Free Luna and Pantheon stay SEPARATE apps; agents get MCP/REST control of each.

## Verified shipped (read from code, not from notes)
- `audiplex_mcp/server.py` (398 lines) — **13 tools**: dj_play_now, dj_skip, dj_queue,
  dj_play_next, dj_reorder, dj_pause, dj_resume, dj_previous, dj_seek, dj_volume,
  dj_play_stream, dj_queue_by, dj_now_playing.
- Server: `playback_bus.py`, `routers/playback.py` (command POST, ~25s long-poll GET,
  state POST/GET), owner-resolved playlists/favorites, dj-agent service token.
- Android: `DjCommandClient.kt`, PlaybackManager add/insert/move + Media3 player volume.
  versionCode 29.
- `radio_free_luna/rfl_mcp/server.py` — **7 tools**: rfl_start_broadcast, rfl_stop,
  rfl_skip, rfl_status, rfl_request_song, rfl_commentary, rfl_context. (= #439 Phase 1)
- `dj_play_stream` = #439 Phase 2 cross-app source switch, commit 3a59349.

## THE DELTA (asked for, does not exist)
- **D1 — #431 DJ persona + on-air voice breaks: ZERO code.** Fully pre-planned in
  `Q:\Pantheon\notes\fable-plans\plan-431.json`: new `routers/dj_voice.py`
  (POST /api/dj/clips multipart + GET /api/dj/clips/{id} reusing `serve_file` for range),
  config `dj_clip_dir`, schema `DjClipCreated`, MCP `dj_announce()` + `dj_break_brief()`,
  `DJ_PERSONA.md` (dayparted personas ported from RFL), Android synthetic negative-track-id
  clip inserted after current song via existing play-next machinery.
  Feasible: Pantheon has TTS (`src/tts.py`, `tts_f5.py`, `tts_service.py`).
  **No DB schema change** (clips on disk; PlaybackCommand.type is free-form → bus untouched).
- **D2 — #439 Phase 3**: Pantheon companion speaker-mute (`/api/companion/speaker`).
  Confirmed absent. Mostly Pantheon-repo work, not audiplex.
- **D3 — non-code gates**: Todd's physical on-device test never done. Every smoke test to
  date used a **simulated** client, so no tool is verified against real Media3.

## Open questions (in plan-back)
1. **Mount state may make all 20 tools unreachable.** `Q:\Pantheon\.mcp.json` mounts ONLY
   `pantheon-orch`. `audiplex-dj` + `rfl-dj` appear only in
   `Q:\Pantheon\migration-staging\configs\jarvis\.mcp.json` (staging). The 07-14 checklist
   claims they're mounted "on the Athena side"; I'm on Solace. If the live config lacks
   them, **wiring beats building**. Not touched — this is the E3 area previously rejected
   by the permission classifier as "unauthorized persistence"; needs explicit sign-off.
2. #431 needs Todd: persona NAME + VOICE sample. Confirm plan-431's reading (port RFL's
   DJ-intelligence as agent-authored TTS breaks; do NOT re-run RFL's own LLM commentary
   loop — the agent is the DJ brain).
3. TTS backend: Pantheon subprocess vs OpenAI-compatible URL? Decides whether audiplex
   takes a dependency on Pantheon.

## Proposed scope (pending signoff)
Primary: build D1 (#431 voice breaks) per plan-431.json.
Secondary (cheap, first): automated end-to-end harness exercising all 13 existing tools
through the real bus on a throwaway port, so on-device becomes a confirmation not the only
evidence.
Not proposed: D2 (other repo), any #429 re-implementation, crossfading (violates audiplex's
no-transcoding rule — plan-431 flags this correctly).

## Constraints honored
- Live server on **:8100 left untouched**; verified UP read-only via curl (HTTP 200).
  Side note: this incidentally satisfies the stale "restart production on 8100" gate that
  was unreachable from Athena — no action needed there.
- Testing will use a throwaway instance on a spare port, never 8100.
  Burned-before gotcha: kill the port's LISTEN pid (`netstat -ano`) or use a fresh port,
  else "restarted" servers silently serve OLD code.
- No schema changes. No commits/pushes. Nothing built.

## Gotchas found this session
- Reading `pantheon.db` suggestions needs UTF-8 stdout wrapping (cp1252 blows up on `↔`).
- Windows Python can't open Git-Bash-style `/q/...` paths — use `Q:\...`.
- Recursive grep across all of `Q:\Pantheon` times out (>120s); scope it to subdirs.

---

# BUILD RESULTS (post-signoff, 2026-08-12)

Approved rulings applied: OQ1 resolved empirically by Jarvis (tools ARE mounted —
he called `dj_now_playing` live from his session; the staging config I found was a
red herring, Jarvis runs from the jarvis persona project). OQ3 = OpenAI-compatible
TTS URL, **no Pantheon dependency** — so plan-431's `dj_synth.py` Pantheon shim was
DROPPED entirely. OQ2 (persona name/voice) is Todd's; everything is persona-agnostic
via env vars so his answer needs no code change.

## Milestone 1 — e2e harness (commit 56c8c5e)
`server/tests/dj_e2e_harness.py`. Real uvicorn on a throwaway port, real JWT through
real auth, real bus, REAL MCP tool functions. Only the device is simulated (mirrors
PlaybackManager/Media3 queue math). **69/69 checks pass.**
Run: `cd server && C:\Python311\python.exe tests/dj_e2e_harness.py`

Proved behaviors nobody had actually checked: play_next inserts AFTER the current
track; reorder keeps the playing item pinned; seek converts s->ms; volume converts
0-100 -> 0.0-1.0 and rejects out-of-range; queue_by resolves artist-by-prefix /
album / genre / **owner's** playlist+favorites; graceful no-match & bad-mode;
play_stream swaps and play_now switches back; no phantom commands.

## Milestone 2 — #431 DJ voice breaks (commits 50a2e59 server+MCP, 1dee9b0 Android)
- **Server:** `routers/dj_voice.py` — POST /api/dj/clips (multipart) + range-capable
  GET /api/dj/clips/{id}; `dj_clip_dir` setting; `DjClipCreated` schema. Clips are
  disk files keyed by epoch-ms, pruned after 7 days. **No schema change**; `announce`
  rides the existing free-form command type so the bus is untouched.
- **MCP:** `dj_break_brief` (dayparted directive + time + optional weather +
  now-playing) and `dj_announce`. **15 tools** now register. `tts_backend.py` =
  OpenAI-compatible `/v1/audio/speech`, `DJ_TTS_CMD` a generic subprocess fallback.
  `dj_persona.py` ports RFL's DAYPART_PERSONAS + hour boundaries (NOT its LLM loop —
  the agent is the DJ brain). `DJ_PERSONA.md` = on-air protocol + writing rules.
- **Android:** `PlaybackManager.insertVoiceClip` + `announce` dispatch in
  `DjCommandClient`. BUILD SUCCESSFUL, **versionCode 33**.
- **Tests:** 10 new server tests (pytest 260 -> **270**); harness grew an announce leg
  with a fake OpenAI-compatible TTS server, asserting the TTS payload contract, that
  the break inserts after the current track without interrupting, that it's a
  synthetic negative-id item, and that the device can fetch the exact audio back
  with range support.

## PLAN BUG FOUND AND FIXED
plan-431 specced the synthetic track id as `-clipId`, but `clip_id` is epoch-ms
(~1.8e12) and `TrackSchema.id` is an **Int** — that overflows. Used a
session-monotonic negative counter instead; the real clipId still rides in the
mediaId as `djclip:<id>`.

## Constraints honored
Live :8100 verified HTTP 200 before AND after all work — never touched. Harness picks
a free port and hard-refuses 8100. The harness writes an explicit jwt_secret into its
temp config because `get_settings()` PERSISTS a generated one and the live
`server/config.yaml` is a candidate path.

## BLOCKER — Radio Free Luna push (needs a human)
The 3-line README pointer is committed locally (d3e76fb) but **NOT pushed**: RFL was
already **ahead 8, behind 3** before I touched it, and README.md diverges upstream.
Reconciling 8 commits I didn't write is not this assignment. Someone who knows that
repo's state should pull/rebase and push. Audiplex itself is fully pushed.

## Remaining (not code)
Todd's on-device gate — now narrowed to "does Media3 physically move audio", since the
plumbing is machine-verified. Checklist updated at
`Q:\Pantheon\notes\2026-07-14-audiplex-dj-test-checklist.md` with the announce leg.
For voice breaks on-device, `DJ_TTS_URL` must be set in the audiplex-dj MCP env.

## Environment gotchas (cost real time)
1. Bare `python` in a Pantheon worker shell = `Q:\Pantheon\.venv-omnivoice`, which
   LACKS passlib. Audiplex needs **C:\Python311\python.exe** (what launch.bat uses).
2. `get_settings()` persists a generated jwt_secret to config.yaml — see above.
3. Reading pantheon.db suggestions needs UTF-8 stdout wrapping (cp1252 chokes on ↔).
4. Windows Python can't open Git-Bash `/q/...` paths — use `Q:\...`.
5. Recursive grep over all of `Q:\Pantheon` times out (>120s); scope it.
