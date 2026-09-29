"""Render the fallback brown-noise sleep bed (#3367) into the meditation library.

    python server/scripts/generate_brown_noise.py [--out FILE] [--minutes 30]

Writes a seamless AAC .m4a (via ffmpeg) that the meditation scanner picks up as
a Book, so the phone streams it through /api/stream/{book_id} like any other
library item. Deterministic noise (fixed seed); needs numpy, scipy and ffmpeg.
"""

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER_DIR))

from audiplex import noise_synth  # noqa: E402

DEFAULT_OUT = Path("Q:/meditations/audio/Brown Noise - Sleep Loop.m4a")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--minutes", type=float, default=30.0)
    args = parser.parse_args()

    samples = noise_synth.brown_noise(args.minutes * 60)
    with tempfile.TemporaryDirectory() as tmp:
        wav = noise_synth.write_wav(samples, Path(tmp) / "brown.wav")
        args.out.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav),
             "-c:a", "aac", "-b:a", "64k",
             "-metadata", "title=Brown Noise - Sleep Loop",
             "-metadata", "artist=Audiplex sleep bed (#3367)",
             str(args.out)],
            check=True,
        )
    print(f"wrote {args.minutes:g} min brown-noise loop to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
