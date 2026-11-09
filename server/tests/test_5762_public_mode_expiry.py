"""#5762: public (party) DJ mode ends by itself: at its public_until (default 23:59 tonight) or when the set stops."""
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from audiplex_mcp import server as mcp, dj_bridge_watcher as w  # noqa: E402

from audiplex import public_mode  # noqa: E402


def _now():
    return datetime.now().astimezone()


def _patter_path() -> Path:
    return Path(os.environ["DJ_PATTER_FILE"])


def _write(data: dict) -> None:
    _patter_path().write_text(json.dumps(data), encoding="utf-8")


def _read() -> dict:
    return json.loads(_patter_path().read_text(encoding="utf-8"))


def _base() -> dict:
    return {"on": True, "every_min": 2, "every_max": 3}


def test_public_live_cases():
    """#5762: public_live is true only for a public flag with a future public_until."""
    now = _now()
    future = (now + timedelta(hours=1)).isoformat()
    past = (now - timedelta(hours=1)).isoformat()
    naive_future = (datetime.now() + timedelta(hours=1)).isoformat()
    assert w.public_live({"public": True, "public_until": future}) is True
    assert w.public_live({"public": True, "public_until": past}) is False
    assert w.public_live({"public": True}) is False
    assert w.public_live({"public": False, "public_until": future}) is False
    assert w.public_live({"public": True, "public_until": "garbage"}) is False
    assert w.public_live({"public": True, "public_until": naive_future}) is True


def test_load_settings_drops_expired_public():
    """#5762: load_settings omits public when public_until is already past."""
    yesterday = (_now() - timedelta(days=1)).isoformat()
    _write({"on": True, "public": True, "public_until": yesterday, "voice": "chris"})
    settings = w.load_settings()
    assert "public" not in settings
    assert settings["on"] is True


def test_public_default_until_is_tonight_2359():
    """#5762: dj_patter(public=True) with no until ends at 23:59 today."""
    _write(_base())
    result = asyncio.run(mcp.dj_patter(public=True))
    data = _read()
    ends = datetime.fromisoformat(data["public_until"])
    assert data["public"] is True
    assert (ends.hour, ends.minute) == (23, 59)
    assert ends.date() == datetime.now().astimezone().date()
    assert "mode: public until 23:59" in result


def test_public_until_past_time_rolls_to_tomorrow():
    """#5762: an HH:MM already passed today means that time tomorrow."""
    now = _now()
    if now.hour == 0 and now.minute == 0:
        pytest.skip("one minute before now is on the previous day at 00:00")
    _write(_base())
    past_hhmm = (now - timedelta(minutes=1)).strftime("%H:%M")
    asyncio.run(mcp.dj_patter(public=True, until=past_hhmm))
    ends = datetime.fromisoformat(_read()["public_until"])
    assert ends.date() == now.date() + timedelta(days=1)


def test_invalid_until_errors_and_leaves_file_unchanged():
    """#5762: a nonsense until returns ERROR and writes nothing."""
    _write(_base())
    before = _patter_path().read_text(encoding="utf-8")
    result = asyncio.run(mcp.dj_patter(until="nonsense"))
    assert result.startswith("ERROR")
    assert _patter_path().read_text(encoding="utf-8") == before


def test_public_false_ends_public_mode():
    """#5762: dj_patter(public=False) removes public keys and reports private."""
    _write(_base())
    asyncio.run(mcp.dj_patter(public=True))
    assert _read()["public"] is True
    result = asyncio.run(mcp.dj_patter(public=False))
    data = _read()
    assert "public" not in data
    assert "public_until" not in data
    assert "mode: private" in result


def test_on_none_keeps_existing_on():
    """#5762: dj_patter(on=None) leaves the stored on/off value alone."""
    _write(_base())
    asyncio.run(mcp.dj_patter(on=None))
    assert _read()["on"] is True


def test_end_public_mode_clears_keys_once():
    """#5762: end_public_mode clears public keys, keeps others, then reports False."""
    _write({**_base(), "public": True, "public_until": _now().isoformat(), "voice": "chris"})
    assert public_mode.end_public_mode() is True
    data = _read()
    assert "public" not in data and "public_until" not in data
    assert data["voice"] == "chris" and data["on"] is True
    assert public_mode.end_public_mode() is False


def test_end_public_mode_missing_file():
    """#5762: end_public_mode with no patter file returns False without raising."""
    _patter_path().unlink(missing_ok=True)
    assert public_mode.end_public_mode() is False


def test_pool_stop_endpoint_ends_public_mode(client):
    """#5762: DELETE /api/playback/pool ends public mode and reports public_ended."""
    until = (_now() + timedelta(hours=1)).isoformat()
    _write({**_base(), "public": True, "public_until": until, "voice": "chris"})
    resp = client.delete("/api/playback/pool")
    assert resp.status_code == 200
    assert resp.json()["public_ended"] is True
    assert "public" not in _read()
