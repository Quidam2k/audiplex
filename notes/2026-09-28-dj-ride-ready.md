# DJ-ride-ready Audiplex — 2026-09-28 (/goal, marker #ride0928)

Branch `duck-deeper`. Nothing is merged, the phone app is not built, and the live :8100
server has not been restarted. Those three steps are for approval day (bottom of this file).

## What shipped (commits on duck-deeper)
| Commit | What |
|---|---|
| 16aca0e | **Talk guard.** A single `_todd_talking()` reads Pantheon `speech_state.json` by default. play_now/queue/play_next/resume/play_stream/transfer are HELD while Todd talks or types. If the file can't be read, they're held too; if the file is missing, they're sent with a warning. dj_skip now goes through `_enqueue`. Every start-type tool now opens with `RESULT sent= held= skipped_missing=[titles] phone_ack=`. dj_now_playing gets a `missing_on_phone` line. |
| 35b829b | **Owner login test.** A pre-9/23 30-day admin token comes back as a ~100-year `X-Refresh-Token` on its first request, with no password step. No code change was needed. |
| 1aa78a9 | **Server:** `Track.content_kind` (music/podcast/clip/ambient, with a migration). The scanner sets it from a per-root config `content_kind` or from podcast/clip folder names. /mix/plan and /pool drop anything that isn't music. New routes: `/api/playback/history`, `pair-notes`, `content-kind` and `POST playlists`. **Phone source (not built):** a file that 404s or isn't found is skipped (network errors are not), and `player_error` carries `skipped`. Unresolvable ids are named in the ack, and when nothing resolves the ack is `failed`. |
| ba3a45d | **DJ mode.** The brief now shows Previous, Now playing and Next, plus DJ pair notes. New tools: `dj_history`, `dj_pair_note(s)`, `dj_set_kind`, `dj_folder(shuffle/queue/playlist)`. DJ_PERSONA.md updated. RFL design page added. |
| bb1950b | **`audiplex_mcp/TOOLS.md`** catalogs all 67 tools, with a drift test. The audit also turned up two more tools: `dj_outro`/`dj_outro_cancel` (the ride-end outro had no tool) and `dj_cooldown`. |
| 050504e | **`server/tests/ride_rehearsal.py`**: a full ride run on a copy of the live DB and config (`--repeat 3`). Building it exposed a bug: a null ack `detail` got a 422, so the command was redelivered every 60s. The server now accepts null. The phone sends "", so it was never affected. |
| Pantheon b12c707e | A `docs/entry-points.md` row that points at TOOLS.md. |

## Verification
- Full server pytest: **686 passed**.
- Android `compileDebugKotlin testDebugUnitTest`: **BUILD SUCCESSFUL**, 74 unit tests. No assembleDebug, so the versionCode is untouched.
- Ride rehearsal ×3: **3/3 passed** (20/20 checks each).
- `git merge-tree --write-tree master HEAD`: **clean**. There are 38 commits ahead of master.
- TOOLS.md drift test passes.

## Open / not done
- **Codex was over quota until Oct 3.** The rehearsal, TOOLS.md, the pair-notes code and the RFL page were written by hand (the RFL page by a Sonnet subagent). The ledger has one `rejected` verdict (token cr-20260929T032026-268839-20833). The subagent couldn't confirm the ledger write actually landed.
- The phone changes need a build and an install before they take effect. Until then, the phone won't auto-skip a dead file. The server-side filter still keeps dead paths off the phone.
- The RFL song-knowledge flag is design only. RFL's analysis is mostly placeholder; only year, genre and lyrics are real.
- The chime-while-talking check in the rehearsal runs the trigger engine in-process, because real clock time can't be forced on the live ticker. Unit tests cover the same path.
- `replace_upcoming` is not talk-guarded, on purpose. It only changes what plays after the current song and never starts audio.
- dj_pool_set records the pool on the server before its first play_now gets held. A held bike start leaves the pool armed but silent, and calling it again once Todd is done starts it.

## 10-minute phone checklist (after approval-day step 1)
1. Open Audiplex once, with no login prompt expected. That lands the 100-year owner token.
2. Settings → Check for Update → install the new build.
3. Start any music. Confirm dj_now_playing shows it and dj_command_status shows `acked`.
4. While talking to Pantheon (mic open), ask a persona to start music. It should say HELD. Stop talking, and it should start.
5. Ask for "the ride mix" (`dj_pool_set todd-ride-mix`). Skip twice and confirm the pool tops up.
6. Ask for a folder shuffle (`dj_folder <path>`). Confirm the reply starts `RESULT sent=… phone_ack=ok`.
7. Ask for a break. The brief should name the previous, current and next songs.
8. Ask for the outro. The current song should finish, then the outro plays and the player pauses.

## Approval day — exact steps (Todd's go required for each)
1. **Build and publish the phone app:**
   `cmd //c "Q:\Development\audiplex\android\gradlew.bat" assembleDebug --console=plain`
   (this bumps versionCode; the in-app updater serves the new APK from the build dir).
2. **Restart :8100:** `taskkill //F //PID <pid of the :8100 listener>`, then
   `cmd //c "wscript.exe Q:\Development\audiplex\launch-hidden.vbs"`. On startup, the
   migration adds `tracks.content_kind` and `dj_pair_notes`.
3. **Merge:** `git checkout master && git merge --no-ff duck-deeper && git push`. Leave out the
   uncommitted auto-generated `CLAUDE.md` change and the untracked data dirs (`covers/`, `data/`,
   `server/dj_clips/`).
4. The audiplex-dj MCP picks up the new tools the next time each persona's MCP restarts.

## Rehearsal ×3
`tests/ride_rehearsal.py --repeat 3`: **3/3 rides passed, 20/20 checks each** (2026-09-28 ~21:00).
