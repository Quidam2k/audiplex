"""#3504: diag logs the phone's volume; queued tracks without loudness get measured."""

import json
import shutil
import wave

import numpy as np
import pytest

from audiplex import loudness_live, playback_bus
from audiplex.models import Track


def _diag(tmp_path):
    path = tmp_path / "playback-diag.jsonl"
    return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []


def _state(track_id, volume, playing=True):
    return {"playing": playing, "track": {"id": track_id}, "queue_index": 0, "volume": volume}


def test_state_line_carries_volume_and_volume_moves_are_logged(tmp_path):
    bus = playback_bus.PlaybackBus()
    bus.set_state(_state(1, 0.5))
    bus.set_state(_state(1, 0.505))  # heartbeat jitter: no line
    bus.set_state(_state(1, 0.2))  # a duck
    bus.set_state(_state(2, 0.2))  # track change mid-duck
    lines = [l for l in _diag(tmp_path) if l["kind"] in ("state", "volume")]
    assert [(l["kind"], l["track_id"], l["volume"]) for l in lines] == [
        ("state", 1, 0.5), ("volume", 1, 0.2), ("state", 2, 0.2)]


def test_measure_missing_fills_only_unmeasured_music(db_session, sample_track):
    sample_track.loudness_lufs = None
    db_session.commit()
    seen = []

    def fake(path):
        seen.append(path)
        return -17.5

    track_id = sample_track.id
    done = loudness_live.measure_missing([track_id, 999999], lambda: db_session, measure=fake)
    assert done == {track_id: -17.5}
    assert db_session.get(Track, track_id).loudness_lufs == -17.5
    assert loudness_live.measure_missing([track_id], lambda: db_session, measure=fake) == {}
    assert len(seen) == 1


def test_enqueue_schedules_measurement_for_queued_tracks(monkeypatch):
    calls = []
    monkeypatch.setattr(playback_bus, "LIVE_LOUDNESS", True)
    monkeypatch.setattr(loudness_live, "schedule", lambda ids, factory: calls.append(list(ids)))
    bus = playback_bus.PlaybackBus()
    bus._enqueue("queue", {"track_ids": [4, 5]}, source="todd")
    bus._enqueue("pause", {}, source="todd")
    assert calls == [[4, 5]]


def test_schedule_skips_tracks_already_in_flight(monkeypatch):
    started = []
    monkeypatch.setattr(loudness_live, "measure_missing", lambda ids, f: started.append(list(ids)))
    loudness_live._in_flight.add(7)
    try:
        thread = loudness_live.schedule([7, 8, -1], lambda: None)
        thread.join(5)
        assert started == [[8]]
        assert loudness_live.schedule([7], lambda: None) is None
    finally:
        loudness_live._in_flight.discard(7)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_measure_lufs_on_a_real_file(tmp_path):
    path = tmp_path / "tone.wav"
    t = np.arange(48000 * 5) / 48000
    pcm = (0.25 * np.sin(2 * np.pi * 440 * t) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes(pcm.tobytes())
    lufs = loudness_live.measure_lufs(str(path))
    assert lufs is not None and -20 < lufs < -10
