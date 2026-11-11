"""#5697: the phone asks whether public DJ mode is live before it fades a skip."""
import json
from datetime import datetime, timedelta

from audiplex import public_mode


def _now():
    return datetime.now().astimezone()


def test_is_public_live(tmp_path, monkeypatch):
    f = tmp_path / "patter.json"
    monkeypatch.setenv("DJ_PATTER_FILE", str(f))
    assert public_mode.is_public_live() is False  # no file
    f.write_text("not json", encoding="utf-8")
    assert public_mode.is_public_live() is False  # junk
    future = (_now() + timedelta(hours=1)).isoformat()
    past = (_now() - timedelta(hours=1)).isoformat()
    f.write_text(json.dumps({"public": True, "public_until": future}), encoding="utf-8")
    assert public_mode.is_public_live() is True
    f.write_text(json.dumps({"public": True, "public_until": past}), encoding="utf-8")
    assert public_mode.is_public_live() is False  # expired
    f.write_text(json.dumps({"public": True}), encoding="utf-8")
    assert public_mode.is_public_live() is False  # no time
    f.write_text(json.dumps({"public": False, "public_until": future}), encoding="utf-8")
    assert public_mode.is_public_live() is False
