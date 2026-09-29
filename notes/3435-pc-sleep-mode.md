# #3435: sleep mode on the PC renderer (2026-09-29)

The PC renderer (windows_client) now handles bed_play / bed_volume / bed_stop /
sleep_timer / cancel_sleep_timer, with the same payloads as the phone. Sleep mode
from `dj_sleep_start` works when the PC is the active device.

## Finding: VLC's default Windows output shares volume across players
With mmdevice (the default) and with wasapi, every media player in the process
shares ONE volume. Setting player A to 5 also reads back 5 on player B. The
crossfade would have muted the bed along with the book. DirectSound gives each
player its own volume, and a mixed setup works: main on mmdevice, bed on
DirectSound, readbacks independent. The bed player is therefore pinned to
DirectSound (`Player(bed_aout="directsound")`).

## Isolated E2E (windows_client/scripts/e2e_sleep_isolated.py, :8199)
The harness uses a temp config/DB/logs and a temp library holding digitally
silent content. It runs the real Player/BusClient/AuthProxy on real VLC outputs,
so no sound is produced. `--aout=dummy` was dropped because it reads every
volume back as 0. The real MCP `dj_sleep_start()` was called with no args,
0.1 min / 5 s fade.
- dj_sleep_start queued bed_play at 0% (default lookup picked "Brown Noise - Sleep Loop"),
  then sleep_timer with bed_fade_to 50%. Every command acked ok on the PC.
- Timeline, sampled every 250 ms: main at 100 for about 6 s, untouched. Then
  main went 100→80→60→40→…→0 while the bed went 0→2→5→…→48→50 in the same
  steps. Main then paused and the timer thread exited.
- Main MRL was unchanged throughout and its position rose monotonically, so the
  track was never restarted or re-queued.
- Loop check: the real bed was played muted at volume 0 and seeked to 3 s
  before its end (1,800,047 ms). It wrapped to 7,151 ms and kept Playing on the
  same player.
- Level of the real generated bed: mean -26.1 dB, max -12.1 dB.
- A synthetic silent AAC of 8 s is about 4 KB, and VLC stalls on it at 250 ms
  over HTTP. This is a harness artifact only: the real file streams fine.

## Not verified
- No real-speaker listening.
- Unit tests stub VLC.
- The live renderer on Solace was not restarted. It isn't running as of
  2026-09-29, and the new code applies at its next launch.
