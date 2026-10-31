# #5282 slice 1: sleep mix by turning things down (2026-10-31)

Todd 52971: don't push the bed past full, turn the book down and let the phone raise everything.

## Evidence (night of 10/30, playback-diag)
Bed 1213 (Starship, -19.6 LUFS, true peak -9.4) at slider 0.87 = +8.9 dB through LoudnessEnhancer, phone at 7 of 25 for most of the night. Heard balance: book -14 (file -17.05, +3.05), bed -10.7. The enhancer as the distortion source is likely, not proven: nothing measures what he heard.

## What changed (Audiplex app only)
- Bed slider stops at the bed's own level. Bed enhancer and boosted ramp-in removed. Levels are plain player volume; the sliders use a 40 dB taper in the UI only.
- "Book at bedtime": a saved level beside the bed slider. Applies while a timer with a bed is armed, during Hear the mix, and during the hold below. Day dial untouched. Book net = server gain + bedtime level, capped at 0 dB, so no limiter at bedtime.
- "Hear the mix": bed audible under the book while the dialog is open. Closing puts it all back.
- One-time migration: an old boost becomes the same cut on the book. 0.87 gives bed 1.0, book -8.9 dB, net -5.85 dB with this book's +3.05.

## After bedtime: what happens to each sound
He raises the phone at bedtime, and Audiplex does not write STREAM_MUSIC. So the raise is still there afterwards.
- Timer ends: book already faded and paused, bed plays on. Any play button restores the book's level (was silent from lock screen or headset before this).
- Cancel: book ramps over 2 s. If the phone is still above where bedtime started on that output, it ramps to the bedtime level, not the day dial.
- Next morning, Audiplex book, music and DJ clips: same rule. They keep the bedtime cut until the phone is back at its bedtime start on that output, or 16 h after bedtime ended. Other outputs aren't affected.
- Next morning, agent voices and Pantheon sounds: NOT covered. They play in the Pantheon app at the raised volume. Audiplex can't reach them without writing STREAM_MUSIC.
- After 16 h with the phone never lowered: Audiplex is back at day level at the raised volume, so it plays louder than before bedtime.

## Limits
- The output is named by the first connected external device; with two Bluetooth devices connected it can name the wrong one.
- First-night numbers (about 4 steps, about 11 of 25) are estimates from 3 steps = 7 dB on 10/25, on another speaker.
- Fallback if the bed is too quiet at a comfortable phone level: Starship has 9.4 dB of clean headroom; re-render under a new id, per notes/5047-sleep-bed-level.md. Propose it to him, don't slip it in.

## Check after a night
stream_volume and bed_handoff lines in playback-diag now carry book_bedtime_level, book_net_db, bedtime_cut, sleep_hold_index. bed_gain_db is gone because the stage is gone. `sleep_hold` logs when the cut ends.

Release: 1.0.69. Held copy data/held/5282/, rollback data/held/rollback-1.0.68/.
