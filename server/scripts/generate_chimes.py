"""Regenerate the self-synthesized Westminster chime clips (#5499).

    python server/scripts/generate_chimes.py [--out DIR]

Default output is server/assets/chimes (the committed clips the DJ ticker
plays). Deterministic: the same code writes byte-identical files.
"""

import argparse
import sys
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER_DIR))

from audiplex import chime_synth  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=SERVER_DIR / "assets" / "chimes")
    args = parser.parse_args()
    manifest = chime_synth.generate_assets(args.out)
    for name, entry in sorted(manifest.items()):
        print(f"{name}: sound {entry['sound_seconds']}s, file {entry['total_seconds']}s")
    print(f"wrote {len(manifest)} clips + manifest.json + LICENSE.txt to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
