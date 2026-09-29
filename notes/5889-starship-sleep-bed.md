# #5889: Starship Sleeping Quarters is the default sleep bed (2026-09-29)

## Files
- **Live bed:** `Q:\meditations\audio\Starship Sleeping Quarters.m4a`. This is
  a seamless loop made with `server/scripts/make_sleep_loop.py`. It is book 610.
- **Original, moved and not deleted:** `Q:\meditations\source\Starship Sleeping
  Quarters (original).m4a`. `source\` is not a library root, so it can't
  compete in the title lookup.
- **DB backup, taken before the scan:**
  `server/audiplex.db.bak-5889-1790708438`. Its integrity check passed, it
  restored into a scratch DB, and it holds 609 books.

## Seam
The original doesn't click at the sample level. Its tail fades into 19 ms of
zeros, and its head is 73 ms of zeros plus a ~0.75 s fade-in (-46 → -35 → -31
→ -27 dB, against a -26 dB body). Looped as-is, that makes a "breath" every
hour.

The processing trims the edges to within 3 dB of the body (head 0.530 s, tail
0.017 s). It then applies noise_synth's 5 s equal-power loop seam and 10 ms
edge ramps, and encodes AAC 128k with no level change.

Measured result:
- **Head:** flat at -26 dB per 0.25 s window.
- **Seam step:** 0.0002, against a typical p99 step of 0.0015.
- **Internal 5 s join:** max step 0.0025.
- **Level:** mean -26.0 dB, max -16.4 dB (the original was -26.0 / -17.9).
- **Length:** 3594.5 s (59:54).

## Registration
The scan covered the meditation root only, in-process against the live DB,
with no restart:
- 1 book added (610) and 1 chapter added. Every existing book row is
  byte-identical (per-row sha1 before and after): audiobook_clean stays at
  598, and meditation goes from 11 to 12.
- Side effect: `init_db`'s `create_all` added the empty `dj_pair_notes` table
  (#ride0928). The live server's next boot would create it anyway.

## Verified
- On an isolated :8199 server running on a copy of the live DB, a no-arg
  `dj_sleep_start` queued `bed_play /api/stream/610 'Starship Sleeping
  Quarters'`.
- With Starship filtered out, the lookup falls back to book 609 "Brown Noise -
  Sleep Loop".
- A read-only check of live :8100 `_default_sleep_bed` returns 610.
- The lookup already matched both "Star Ship" and "Starship" (plus a "Sleeping
  Quarters" substring), so no lookup code changed.

## Not verified
- No real-ear listening to the loop.
