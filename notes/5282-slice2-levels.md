# #5282 slice 2: a level for each sound (2026-10-31)

Committed and HELD: no build, no release, no :8100 config change or restart. 1.0.70 stays what the updater offers until Jarvis says otherwise after tonight's result. A build in android/app/build/outputs/apk/debug is immediately what the updater offers, so verify with testDebugUnitTest only.

## Shape (Todd 53037, 53042)
Audiplex Settings > Levels: Music, Book, Sleep, DJ, Chimes. Voice stays on the Pantheon app's slider. Book at bedtime stays in the sleep dialog only. No cross-app mixer: /api/mix/* and the audition endpoint are dropped, with no consumer. The only code the same plan would have needed for it (MixCategory wire names) is removed.

Each slider runs silent to +12 dB with a red mark at the sound's own level; Full is sticky within 0.5 dB. Past the mark, the readout turns red: "+6 dB, can distort". Defaults stay at or below full: 1.0, bed 0.9. Book at bedtime stops at full.

## What changed
- MixLevels.kt: pure rules. PlaybackManager.mainSplit is the one level calculation: amplitude split into player volume plus limiter raise, total limiter cap +12 dB.
- DJ voice: clips play at the DJ level, not music dial and track gain. Seeded once from the music dial, so the update changes nothing. Live fallback_lufs is null, so clips had no track gain before either.
- ClipVoiceSteering writes the level at any boundary with a clip on a side; the old path no-ops with normalization off and treats a low voice as a duck.
- Clips fade in over their first 150 ms, covering the moment before the voice level lands.
- Chimes: traced. dj_triggers.clock_tick (and bed_chime cues) send bed_play with {base}/api/dj/chimes/{name}, volume 0.12 from chime settings; DjCommandClient calls djBedPlay, which multiplies by the chime level. Hour strikes use the same path.
- Bed: LoudnessEnhancer back, only past full. It starts at 0 in a timed crossfade; Hear the mix, restart and retry start at the chosen level.
- Server: book_gain_db cuts to target when book_max_boost_db is 0; book gain mode says attenuate_only then.
- Bedtime shift: when book gain mode is attenuate_only, Book at bedtime rises 6 dB, capped at full, recorded so it can't repeat. Undone when the mode goes back. Capped remainder shows as a note in Levels until he moves the slider. Log event: bedtime_shift.

## The flip (not done; his call)
Config: music_target_lufs -20, music_max_boost_db 0, book_target_lufs -20, book_max_boost_db 0, plus restart.
Voice in his headsets (Orolo, speech_route 10/23-10/31): the phone caps headsets at 100%, so Shokz and Arctis get 0 dB by day and +3.52 on the bike. Only the Pixel speaker and JBL Flip 6 get +6. So nominal voice is about -20 by day and -16.5 on the bike in his ears. Voice is RMS and music is LUFS, so all of this is nominal, not measured.
So the flip does not keep the headset balance: music drops about 6 dB against a voice that doesn't move. On speakers the voice still sits above.
Taste it first with no config change: Settings > Music and Book at about -6 dB. That is close to the flip, except quiet tracks still get raised.

## Not verified
- Nothing has played on a phone: JVM tests only. ClipVoiceSteering is tested with a fake session, not PlaybackManager. Skip, previous and resume were traced by reading and two independent reviews.
- Slice 1 has no phone test yet either.
- Slider mark alignment and the 150 ms clip fade have not been seen or heard.

## Known costs
- Two places for one level: Sleep is in Settings and the sleep dialog, the same saved value.
- Past-full music ducks less under a Talk: the duck is on player volume and the limiter stays on. At +12 dB, the 5% duck is about -14 dB rather than -26.
- The DJ `volume` command stops at full, so it overwrites a past-full dial. The state report shows player volume (at most 1).
- After a manual change to Book at bedtime, a rollback undoes the recorded amount from whatever is there, not back to the exact old value.
- Dropped reviewer finding: "update makes the DJ louder" assumed a median fallback; live fallback_lufs is null.
