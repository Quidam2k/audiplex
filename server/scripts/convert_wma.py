"""Convert every .wma under the music roots to an AAC .m4a beside it (#8715).

ExoPlayer can't play WMA, so these tracks never reached the library. Todd's call
(msg 46943): convert them so everything is usable, keep the .wma originals. The
music scanner ignores .wma, so the originals stay out of the index on their own.

Headless: ffmpeg runs as a hidden child, never a window. Idempotent: a track
whose .m4a already exists is skipped. Writes to a temp name and renames, so an
interrupted run never leaves a half-written .m4a for the scanner to index.

    python scripts/convert_wma.py            # convert
    python scripts/convert_wma.py --dry-run  # list only
"""

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

import yaml

SERVER = Path(__file__).resolve().parent.parent
BITRATE = "256k"  # sources are ~128-192k wmav2; 256k AAC loses nothing audible
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def music_roots(config: Path) -> list[str]:
    roots = yaml.safe_load(config.read_text(encoding="utf-8"))["library_roots"]
    return [r["path"] for r in roots if r.get("category") == "music"]


def find_wma(roots: list[str]) -> list[Path]:
    found = []
    for root in roots:
        for d, _, files in os.walk(root):
            found += [Path(d) / f for f in files if f.lower().endswith(".wma")]
    return sorted(found)


def convert(src: Path) -> None:
    dst = src.with_suffix(".m4a")
    tmp = src.with_name(src.stem + ".converting")  # not *.m4a, so a scan can't catch it half-written
    try:
        subprocess.run(
            ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
             "-i", str(src), "-map", "0:a", "-map_metadata", "0",
             "-c:a", "aac", "-b:a", BITRATE, "-movflags", "+faststart", "-f", "ipod", str(tmp)],
            check=True, creationflags=NO_WINDOW, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as e:
        tmp.unlink(missing_ok=True)  # never leave a partial file for the scanner
        raise RuntimeError((e.stderr or "").strip()[-300:] or f"ffmpeg exit {e.returncode}")
    # A just-written file is often held for a moment (AV scan), so retry the rename.
    for attempt in range(10):
        try:
            os.replace(tmp, dst)
            return
        except PermissionError:
            if attempt == 9:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=SERVER / "config.yaml")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    done = skipped = failed = 0
    for src in find_wma(music_roots(args.config)):
        if src.with_suffix(".m4a").exists():
            skipped += 1
            continue
        if args.dry_run:
            print(f"would convert: {src}")
            continue
        try:
            convert(src)
            done += 1
            print(f"converted: {src}")
        except (RuntimeError, OSError) as e:
            failed += 1
            print(f"FAILED: {src}: {e}", file=sys.stderr)
    print(f"converted={done} skipped_existing={skipped} failed={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
