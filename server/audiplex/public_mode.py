"""#5762: public (party) DJ mode ends when the set stops.

The mode lives in the repo's data/dj_patter.json ('public' + 'public_until', written by
the dj_patter tool). Stopping the pool (dj_pool_stop, a scheduled stop) is the event
being over, so it clears both keys. Best-effort: a stop never fails over this file.
"""
import json
import os
from pathlib import Path


def patter_file() -> Path:
    if os.environ.get("DJ_PATTER_FILE"):
        return Path(os.environ["DJ_PATTER_FILE"])
    return Path(__file__).resolve().parents[2] / "data" / "dj_patter.json"


def end_public_mode() -> bool:
    """Clear public mode. True only if it was set."""
    path = patter_file()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or not ({"public", "public_until"} & raw.keys()):
            return False
        raw.pop("public", None)
        raw.pop("public_until", None)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(raw), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except (OSError, ValueError) as e:
        print(f"[public_mode] could not end public DJ mode: {e}", flush=True)
        return False
