# #5047: the sleep bed is louder at the handoff (2026-10-26)

## Evidence (night of 10/25)
- Book 158 was playing at -17.05 LUFS, served +3.05 dB, so about -14 LUFS as heard.
- Todd used the phone sleep button. The fade ran 21:41:58–21:43:57 PDT.
- Pantheon `device_logs` `vol_change` (phone, A2DP Soundcore Flare+) shows STREAM_MUSIC 10 → 13 of 25 at 21:44:04–36, seven seconds after the handoff. The in-app bed slider was already at max.
- Bed 610 (Starship) measured -26.6 LUFS, about 12.6 dB under the book. On Android's default media curve, +3 of 25 steps ≈ +7 dB. That is an estimate for this speaker.

## Change (live, no restart, no app needed)
| bed | old id / LUFS | new id / LUFS | true peak |
|---|---|---|---|
| Starship Sleeping Quarters (default) | 610 / -26.6 | 1213 / -19.6 (+7.0 dB) | -9.4 |
| Brown Noise - Sleep Loop (fallback) | 609 / -30.9 | 1212 / -21.2 (limiter at -1.5 dBTP) | -2.2 |

- Brown noise sits 1.6 dB under Starship (Jarvis ruling). Starship sits 5.6 dB under the book as heard.
- Loop seams are clean. Starship's seam step is 0.0004 against a p99 sample step of 0.0038; brown noise is 0.0077 against 0.0501.
- New ids are on purpose: the phone plays a cached copy keyed on `/api/stream/<id>` (`SleepBedRules.cacheFileName`), so a file replaced in place would never reach it.
- Originals: `Q:\meditations\source\* (… pre-5047).m4a`. DB backup: `server/audiplex.db.bak-5047-1793037828`.
- To restore: move the two source files back to `audio\` under the plain names and rescan the meditation root only. The rescan gives fresh ids, so the phone picks the change up.
- To trim (if Todd turns the bed down by hand): re-render from the source copies with a smaller `volume=`, then swap and rescan the same way.

## App 1.0.66 (logging, plus the #4906 fix)
- `stream_volume` client-log line: media-volume changes while Audiplex plays or sleep mode is armed. Presses within 2 s are coalesced into one line. The line carries before, after, max, kind, book gain, bed state/level and sleep_armed.
- `bed_handoff` line when the sleep fade ends into a bed.
- `boot_link_start` line from BootReceiver.
- #4906 root cause: `ApiServiceHolder` got its server URL only when a screen opened. After a reboot or a UI-less process restart, the DJ link sat UNCONFIGURED. `AudiplexApp.onCreate` now sets the saved URL (`setBaseUrlIfUnset`, synchronized).
- Held copy of the APK: `data/held/5047/audiplex-1.0.66-66.apk`.
