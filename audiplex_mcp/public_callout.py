"""#5648 public DJ mode: speak ONE fixed-template transition callout in the DJ voice.

Todd 11/07 (msg 57242): in public mode the personas do no DJ stuff; the DJ voice
only says "That was X. Cheers." at a transition, every few songs. Pantheon's
dj_bridge_push.py builds the text and runs this as a subprocess, so Audiplex
still never imports Pantheon (#439) and the line goes through dj_announce's own
gates (quiet time/DND, Todd talking, the announce gate) and its one render path.

No bank-clip fallback (fallback_kind=""): a stock clip is a different voice, and
in a public room a failed render must mean silence, never someone else talking.

    python -m audiplex_mcp.public_callout --text "That was X. Cheers." --voice chris [--dry-run]

--dry-run renders the clip and reports it; nothing is uploaded or queued.
Exit 0 = queued (or rendered, dry run), 1 = refused/failed (reason on stdout).
"""
import argparse
import asyncio
import os
import sys

# The caller supplies the same env the personas' audiplex-dj MCP has (AUDIPLEX_URL,
# DJ_TTS_CMD, DJ_SPEECH_STATE_FILE, PANTHEON_TTS_REMOTE_URL...); none is hardcoded here.
REQUIRED_ENV = ("AUDIPLEX_URL", "DJ_TTS_CMD")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="public_callout")
    ap.add_argument("--text", required=True)
    ap.add_argument("--voice", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    missing = [key for key in REQUIRED_ENV if not os.environ.get(key)]
    if missing:
        print(f"not configured: set {', '.join(missing)}")
        return 1
    os.environ["DJ_TTS_VOICE"] = args.voice  # the callout's voice, whatever the MCP env says

    from audiplex_mcp import server, tts_backend  # after the env: server reads AUDIPLEX_URL at import

    if args.dry_run:
        try:
            path = asyncio.run(tts_backend.synthesize(args.text))
        except Exception as exc:
            print(f"render failed: {exc}")
            return 1
        print(f"dry-run rendered {path} in voice {args.voice!r}; nothing queued")
        return 0

    result = asyncio.run(server.dj_announce(args.text, mode="next", title="DJ callout", fallback_kind=""))
    print(result)
    return 0 if result.startswith("Queued") else 1


if __name__ == "__main__":
    sys.exit(main())
