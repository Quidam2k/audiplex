"""#2806: the PC crossfade on REAL libvlc, silently (dummy audio output).

tests/ stubs VLC, so this is the check that the player swap, event detach and
ramp work against the real library. Makes no sound, touches no server.

  C:\Python311\python.exe windows_client/scripts/check_crossfade_real_vlc.py

The dummy output drains its last ~1 s instantly, hence 8 s tones and a 3 s fade.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from audiplex_pc.player import Player, QueueItem  # noqa: E402


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="audiplex-xfade-"))
    urls = []
    for n in (1, 2, 3):
        f = tmp / f"{n}.wav"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                        f"sine=frequency={220 * n}:duration=8", str(f)], check=True)
        urls.append(f.as_uri())
    player = Player(vlc_args=["--aout=dummy"], bed_aout=None)
    seen = []
    try:
        player.play_now([QueueItem("track", n, f"t{n}", "a", u) for n, u in zip((1, 2, 3), urls)])
        player.set_crossfade(3)
        deadline = time.monotonic() + 9.5
        while time.monotonic() < deadline:
            seen.append((player.index, player._xfade_old is not None))
            time.sleep(0.05)
    finally:
        player.set_crossfade(0)
        player.stop()
    overlap = sum(1 for s in seen if s == (1, True)) * 0.05
    ok = overlap >= 1.0 and seen[-1] == (1, False)
    print(f"overlap {overlap:.1f}s, final index {seen[-1][0]}, fading at end {seen[-1][1]}")
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
