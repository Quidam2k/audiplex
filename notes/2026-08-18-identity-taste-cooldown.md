# Song identity, listening signal, recency cooldown — Phase A

**Items:** #943 (partial — identity layer only), #947, #948
**Assignments:** #3034, #3035, executed under the approved plan-back #9258 (#3036)
**Date:** 2026-08-18
**Ships as:** server + MCP only. No APK. 1.0.38 is unaffected and still ships alone.

---

## What was actually missing

The audit that preceded this work is the part worth keeping, because it cut the
job roughly in half.

`PlayStat` has been **row-per-event since long before any of this** — `start`,
`complete`, `skip`, `stop`, each with the real position at the discontinuity.
So against Todd's two asks in #947:

- *"skip timing, a skip inside ~10s means strong dislike"* — already captured
  AND already implemented. `EARLY_SKIP_THRESHOLD_SECONDS` is literally 10.0 and
  has been driving likely-skips since before 3a.
- *"full-play completion counts"* — already captured as `event='complete'`, and
  already counted by most-played.

**Nothing new needed capturing on the client.** What was missing was
aggregation and exposure, all server-side. That is what this delivers.

## The identity layer — two keys, deliberately

`server/audiplex/identity.py`. No MusicBrainz IDs exist here, so identity is
derived from (title, artist, duration, file_hash).

- **`recording_id`** — the same audio. Ratings and play statistics bind here.
  Union-find over the catalog: identical `file_hash` merges unconditionally
  (same bytes is the strongest evidence there is), otherwise same normalized
  title + artist with durations within 3 seconds.
- **`work_id`** — the same song in any version. Cooldown binds here. Normalized
  title with bracketed qualifiers stripped, plus a trailing `- <qualifier>` only
  when the tail actually reads like a version marker, so `Life - A Portrait`
  survives while `Barracuda - 2004 Remaster` does not.

They are not one key and that is the entire point. A live cut and the studio
take must share a **cooldown** — Todd doesn't want the song twice in twenty
minutes in any form — but must NOT share a **rating**. Loving the studio cut and
finding the live one thin is an ordinary opinion, and one key averages it away
silently. Same class of loss #3028 avoided by refusing to convert `dj_rate`'s
vocabulary.

The cluster label is derived from the cluster's own content, never from a row
id, so when #943 later syncs a phone copy the recording does not get renamed out
from under the ratings already bound to it.

## What the real library says about it

Ran the identity map over the live 217-track catalog. Two findings, both worth
knowing before anyone trusts this too far:

**207 of 217 tracks have a blank artist tag.** The library is the flat untagged
dump `dj_library` already warns about — the artist is inside the *title*
(`"Billie Eilish - bury a friend (Lyrics)"`). So in practice most work keys are
title-only.

The consequence, stated plainly: **two different artists covering the same song,
both untagged, would merge at work level** and one would be wrongly cooled down.
It cannot corrupt taste data — ratings bind to `recording_id`, which also uses
duration — so the blast radius is a track occasionally passed over, not an
opinion destroyed. Acceptable, but it is a real limit, not a hypothetical one.

**Work clustering found exactly 3 merges in the live library, all correct:**

- `bury a friend (Lyrics)` + `bury a friend (Official Music Video)`
- `We Know The Way` + `We Know The Way (From "Moana")`
- `Concrete Wall (Pumpkin Remix)` + `Concrete Wall (Pumpkin Remix) [Free]`

Zero false merges, and zero multi-track recording clusters (the YouTube rips
differ in length by more than the 3s tolerance). Those three are also literal
duplicate rips — worth a cleanup pass someday, unrelated to this work.

## #947 — aggregation, not capture

`server/audiplex/taste.py`, exposed at `GET /api/music/track-stats` (caller) and
`GET /api/playback/track-stats` (owner-scoped, for the DJ).

- **completion rate**, not raw completes. "Played eight times, finished twice"
  and "played twice, finished twice" have the *same play count* and opposite
  meanings; the rate is what separates them. Null when never started — `0.0`
  would read as "he never finishes it" when the truth is "no evidence".
- **mean/median skip position** — where it loses him. Bailing at 4 seconds is
  "wrong song"; bailing at four minutes is "good song, wrong moment".
- Everything pools by `recording_id`, so a track that exists both on the server
  and (later) on the phone doesn't look half-listened-to twice.

Aggregated in Python, not SQL: a median has no portable SQLite expression, and
grouping by a *derived* identity would mean pushing the identity rules into the
query. A few hundred tracks makes that trade free.

**The caveat that must not get lost:** `complete` is posted with the track's
full duration, not measured listening. It means **reached the end**, not *heard
all of it*. Adequate for taste; it is not proof of attention and nobody
downstream should later mistake it for that.

## #948 — cooldown is a query, not a store

No new storage at all. `PlayStat` already has timestamps.

- `GET /api/playback/cooldown` — what the owner heard inside the window.
- `POST /api/playback/candidates/filter` — the real one: hand it candidate
  track ids, get back `allowed` plus `suppressed`, each suppression carrying
  `reason` (`recording_cooldown` / `work_cooldown` / `low_rating`), a sentence
  the DJ can say out loud, and `clears_in_minutes`.
- 20 minutes at both levels per Todd, tunable per-request and via
  `dj_recording_cooldown_minutes` / `dj_work_cooldown_minutes` in config.yaml.
- An early skip still counts as recently heard. He heard the front of it two
  minutes ago *and* disliked it — both are reasons not to replay it now.

**Advisory, never a veto.** This is the design decision most worth remembering.
`dj_play_now` / `dj_queue` / `dj_play_next` consult the filter and append a
heads-up line naming what just played — and then queue it anyway. When Todd asks
for a song by name he gets that song. Cooldown exists to stop the DJ repeating
*itself*, not to overrule him. The advisory read is also wrapped so that a
failure to reach it can never stop a command from being sent.

Suppressions travel *with their reason* for the same purpose the ACK exists: a
silently shortened candidate list teaches the DJ nothing and leaves Todd
wondering why it never plays something he likes.

## MCP surface

Two new tools (15 → 17): **`dj_track_stats`** (completion rates, where skips
land) and **`dj_check_picks`** (would this set repeat something, and why).

## Verification

- **345 server tests pass** (314 before + 31 new in `test_identity_taste.py`).
- **DJ e2e harness: 80/80**, including 12 new checks on the real stack — the
  live cut caught at work level while staying a separate recording, the
  completion rate read back as `25% finished (1/4 plays)`, and the advisory
  proven advisory: queueing a just-played track emits the heads-up **and the
  command still reaches the device**.
- One pre-existing harness check needed updating, not because it broke but
  because the fixture gained a fourth Miles Davis track: `dj_queue_by(artist)`
  correctly returns 4 now.
- **Live server on :8100 restarted onto this code** and the new endpoints
  exercised over HTTP with the real DJ token against the real 217-track
  library. The phone was mid-poll at restart; the FGS reattaches, as proven
  on 08-18.

## Not done, deliberately

- **#943 Phase B (local library) is NOT built.** It needs an APK — MediaStore
  scan, `READ_MEDIA_AUDIO`, catalog upload, prefer-local playback. The identity
  layer it depends on is now in place and ready for it.
- Todd's future errand to re-import his full library to Solace stays
  out-of-scope context, per the assignment.
- The 08-14 CRASH verdict in `notes/2026-08-14-dj-playback-nondelivery.md`
  remains open and untouched. It still cannot be answered until 1.0.38 is
  installed.
