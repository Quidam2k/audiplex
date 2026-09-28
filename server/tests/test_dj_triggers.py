"""DJ trigger engine (#5480 cue clips, #5499 clock chimes, #5515 ride-end outro).

Everything runs against the in-process bus + the isolated pool state file
(conftest). Nothing here reaches a live server or a phone: the speech-state
file is a temp file, the chime base URL is a fake host, and scheduled
callbacks (after-clip pause, chime bed_stop) are captured instead of timed.
"""

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from audiplex import dj_pool, dj_triggers, playback_bus
from audiplex.database import _migrate_create_dj_mix_specs
from audiplex.playback_bus import bus
from audiplex.routers import dj_voice

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from audiplex_mcp import server as mcp_server  # noqa: E402

BASE = "http://phone-sees-this:8100"


# ----- fixtures / helpers -----


@pytest.fixture(autouse=True)
def engine(tmp_path, monkeypatch):
    """Temp speech file, no live DB for the pool hook, captured scheduling."""
    speech = tmp_path / "speech_state.json"
    monkeypatch.setenv("DJ_SPEECH_STATE_FILE", str(speech))
    monkeypatch.delenv("AUDIPLEX_PUBLIC_URL", raising=False)
    monkeypatch.setattr(dj_triggers, "_client_base_url", BASE)
    monkeypatch.setattr(playback_bus, "_pool_session", lambda: None)
    scheduled = []
    monkeypatch.setattr(dj_triggers, "_schedule", lambda delay, fn: scheduled.append((delay, fn)))
    dj_triggers.reset_warnings()
    bus.reset()
    yield {"speech": speech, "scheduled": scheduled}
    bus.reset()


def _speech(engine, talking=False, spoke=None, omit_spoke=False):
    data = {"stt_active": talking, "talk_active": False, "composing": False}
    if not omit_spoke:
        data["todd_last_spoke_at"] = spoke
    engine["speech"].write_text(json.dumps(data), encoding="utf-8")


def _iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _state(current, after=(), playing=True, title="song", position_ms=0, duration_ms=0):
    queue = [{"index": 0, "id": current, "title": title}] + [
        {"index": i + 1, "id": t, "title": f"t{t}"} for i, t in enumerate(after)
    ]
    return {
        "playing": playing,
        "track": {"id": current, "title": title},
        "queue_index": 0,
        "queue": queue,
        "position_ms": position_ms,
        "duration_ms": duration_ms,
    }


def _pool(active=True):
    pool = dj_pool.get_pool()
    pool.state["active"] = active
    return pool


def _cue(cid, kind, track_id, clip_id=900, rendered_at=None, **extra):
    return {
        "id": cid,
        "trigger": {"kind": kind, "track_id": track_id},
        "clip_id": clip_id,
        "clip_title": f"DJ break \u00b7 Jarvis #{cid}",
        "clip_duration": 5.0,
        "say": f"patter {cid}",
        "rendered_at": rendered_at if rendered_at is not None else time.time(),
        "status": "pending",
        "done": False,
        "held_boundaries": 0,
        **extra,
    }


def _cmds(kind=None):
    return [r for r in bus._commands.values() if kind is None or r.type == kind]


# ----- track_end / track_start placement -----


def test_track_end_fires_once_when_its_track_becomes_current(engine):
    _speech(engine)
    pool = _pool()
    pool.state["pending_cues"] = [_cue(1, "track_end", 10)]
    bus.set_state(_state(5, after=[10, 11]))
    assert _cmds("announce") == []
    bus.set_state(_state(10, after=[11]))
    ann = _cmds("announce")
    assert len(ann) == 1
    assert ann[0].payload["mode"] == "next"  # lands after 10, never mid-song
    assert ann[0].payload["clip_id"] == 900
    assert ann[0].payload["clip_url"] == "/api/dj/clips/900"
    assert ann[0].payload["title"] == "DJ break \u00b7 Jarvis #1"
    bus.set_state(_state(10, after=[11]))  # the phone repeating itself
    bus.set_state(_state(11))
    assert len(_cmds("announce")) == 1
    assert pool.state["pending_cues"][0]["status"] == "fired"
    assert not _cmds("play_now")


def test_track_start_fires_when_the_predecessor_is_current(engine):
    _speech(engine)
    pool = _pool()
    pool.state["pending_cues"] = [_cue(2, "track_start", 12)]
    bus.set_state(_state(10, after=[11, 12]))
    assert _cmds("announce") == []  # 12 is not next yet
    bus.set_state(_state(11, after=[12]))
    assert len(_cmds("announce")) == 1
    bus.set_state(_state(12))
    assert len(_cmds("announce")) == 1


def test_play_track_goes_before_the_clip_and_legacy_top_up_leaves_engine_cues(engine):
    _speech(engine)
    pool = _pool()
    pool.state["pending_cues"] = [_cue(3, "track_end", 10, play_track=77)]
    res = pool.top_up(current_track_id=11, upcoming_track_ids=[11], previous_track_id=10)
    assert 77 not in res["picks"] and pool.state["pending_cues"][0]["status"] == "pending"
    bus.set_state(_state(10))
    types = [r.type for r in _cmds() if r.type in ("play_next", "announce")]
    assert types == ["play_next", "announce"]  # clip inserted after, so it plays first
    assert _cmds("play_next")[0].payload["track_ids"] == [77]


# ----- hold guard -----


def test_held_while_talking_then_fires_at_the_next_boundary(engine):
    _speech(engine, talking=True)
    pool = _pool()
    pool.state["pending_cues"] = [_cue(4, "track_end", 10)]
    bus.set_state(_state(10, after=[11]))
    cue = pool.state["pending_cues"][0]
    assert cue["status"] == "held" and cue["held_boundaries"] == 1
    assert _cmds("announce") == []
    _speech(engine, talking=False)
    bus.set_state(_state(11))
    assert len(_cmds("announce")) == 1
    assert cue["status"] == "fired"


def test_dropped_after_two_missed_boundaries(engine):
    _speech(engine, talking=True)
    pool = _pool()
    pool.state["pending_cues"] = [_cue(5, "track_end", 10)]
    bus.set_state(_state(10, after=[11, 12]))
    bus.set_state(_state(11, after=[12]))
    cue = pool.state["pending_cues"][0]
    assert cue["status"] == "dropped" and cue["drop_reason"] == "held_too_long"
    _speech(engine, talking=False)
    bus.set_state(_state(12))
    assert _cmds("announce") == []


def test_dropped_when_todd_spoke_after_the_clip_was_rendered(engine):
    rendered = time.time() - 300
    _speech(engine, spoke=_iso(rendered + 60))
    pool = _pool()
    pool.state["pending_cues"] = [_cue(6, "track_end", 10, rendered_at=rendered)]
    bus.set_state(_state(10))
    cue = pool.state["pending_cues"][0]
    assert cue["status"] == "dropped" and cue["drop_reason"] == "stale"
    assert _cmds("announce") == []


def test_fresh_clip_fires_when_todd_spoke_before_it(engine):
    rendered = time.time() - 300
    _speech(engine, spoke=_iso(rendered - 60))
    pool = _pool()
    pool.state["pending_cues"] = [_cue(7, "track_end", 10, rendered_at=_iso(rendered))]
    bus.set_state(_state(10))
    assert len(_cmds("announce")) == 1


@pytest.mark.parametrize("omit", [True, False])
def test_staleness_fails_open_when_last_spoke_is_absent_or_null(engine, omit, caplog):
    _speech(engine, spoke=None, omit_spoke=omit)
    pool = _pool()
    pool.state["pending_cues"] = [_cue(8, "track_end", 10, rendered_at=1.0)]  # ancient clip
    with caplog.at_level("INFO", logger="audiplex.dj_triggers"):
        bus.set_state(_state(10))
    assert len(_cmds("announce")) == 1
    assert "fail-open" in caplog.text


def test_missing_speech_file_is_fail_open(engine):
    pool = _pool()  # no speech file written
    pool.state["pending_cues"] = [_cue(9, "track_end", 10)]
    bus.set_state(_state(10))
    assert len(_cmds("announce")) == 1
    assert dj_triggers.read_speech_state()["readable"] is False


def test_unset_env_defaults_to_the_pantheon_speech_file(engine, monkeypatch, tmp_path):
    default = tmp_path / "pantheon_speech_state.json"
    default.write_text(json.dumps({"talk_active": True}), encoding="utf-8")
    monkeypatch.delenv("DJ_SPEECH_STATE_FILE", raising=False)
    monkeypatch.setattr(dj_triggers, "DEFAULT_SPEECH_STATE_FILE", str(default))
    assert dj_triggers.speech_state_path() == default
    assert dj_triggers.read_speech_state()["talking"] is True  # read, not fail-open
    pool = _pool()
    pool.state["pending_cues"] = [_cue(10, "track_end", 10)]
    bus.set_state(_state(10))
    assert pool.state["pending_cues"][0]["status"] == "held"


def test_the_default_path_constant_is_pantheons():
    assert dj_triggers.DEFAULT_SPEECH_STATE_FILE == "Q:/Pantheon/data/runtime/speech_state.json"


# ----- never over a stream or a DJ break -----


def test_no_fire_during_a_stream_or_dj_break(engine):
    _speech(engine, talking=True)
    pool = _pool()
    pool.state["pending_cues"] = [_cue(11, "track_end", 10), _cue(12, "track_start", 30)]
    bus.set_state(_state(10, after=[20]))  # cue 11 held
    _speech(engine, talking=False)
    bus.set_state(_state(-1, title="Radio Free Luna"))  # stream
    bus.set_state(_state(-7, after=[30], title="DJ break"))  # a voice break before 30
    assert _cmds("announce") == []
    assert pool.state["pending_cues"][0]["status"] == "held"
    assert pool.state["pending_cues"][0]["held_boundaries"] == 1
    bus.set_state(_state(20, after=[30]))  # back on music: both go
    assert len(_cmds("announce")) == 2


def test_a_state_report_survives_an_engine_failure(engine, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("engine exploded")

    monkeypatch.setattr(dj_triggers, "on_state", boom)
    bus.set_state(_state(10))
    assert bus.get_state()["track"]["id"] == 10


# ----- clip pinning -----


def test_pending_cue_clip_is_pinned_from_prune_until_fired(engine, tmp_path):
    _speech(engine)
    clips = tmp_path / "clips"
    clips.mkdir()
    old = time.time() - (dj_voice.CLIP_TTL_DAYS + 1) * 86400
    for cid in (111, 222):
        f = clips / f"{cid}.wav"
        f.write_bytes(b"RIFF")
        os.utime(f, (old, old))
    pool = _pool()
    pool.state["pending_cues"] = [_cue(13, "track_end", 10, clip_id=111)]
    dj_voice._prune(clips)
    assert (clips / "111.wav").exists()
    assert not (clips / "222.wav").exists()

    bus.set_state(_state(10))  # fires; the phone fetches the clip when it plays
    assert 111 in dj_triggers.pinned_clip_ids()
    later = time.time() + dj_triggers.CLIP_PIN_GRACE_S + 10
    assert 111 not in dj_triggers.pinned_clip_ids(now=later)
    dj_voice._prune(clips, pinned=dj_triggers.pinned_clip_ids(now=later))
    assert not (clips / "111.wav").exists()


# ----- clock chimes -----


def _at(hh, mm, ss):
    return datetime.now().replace(hour=hh, minute=mm, second=ss, microsecond=0).timestamp()


def _playing_at(now, track=10):
    bus.set_state(_state(track))
    st, _ = bus._states["phone"]
    bus._states["phone"] = (st, now)  # fresh as of the fake clock


def test_chime_fires_only_at_the_quarters_in_dj_mode(engine):
    _speech(engine)
    _pool()
    now = _at(10, 15, 3)
    _playing_at(now)
    res = dj_triggers.clock_tick(bus, now=now)
    assert res["chime"] == "fired"
    plays = _cmds("bed_play")
    assert len(plays) == 1
    assert plays[0].payload["url"] == f"{BASE}/api/dj/chimes/q15"
    assert plays[0].payload["volume"] == pytest.approx(0.12)
    assert dj_triggers.clock_tick(bus, now=now + 4)["chime"] == "none"  # same quarter
    # Between quarters nothing new: 10:22 belongs to the 10:15 slot, already handled.
    assert dj_triggers.clock_tick(bus, now=_at(10, 22, 0))["chime"] == "none"
    assert len(_cmds("bed_play")) == 1


def test_chime_bed_stop_after_the_clip_duration(engine):
    _speech(engine)
    _pool()
    now = _at(9, 30, 1)
    _playing_at(now)
    dj_triggers.clock_tick(bus, now=now)
    manifest = json.loads((dj_triggers.chime_dir() / "manifest.json").read_text())
    (delay, fn), = engine["scheduled"]
    assert delay == pytest.approx(manifest["q30"]["sound_seconds"] + dj_triggers.CHIME_STOP_PAD_S)
    assert _cmds("bed_stop") == []
    fn()
    assert len(_cmds("bed_stop")) == 1


def test_chime_stop_leaves_a_sleep_bed_that_started_meanwhile(engine):
    _speech(engine)
    _pool()
    now = _at(9, 45, 1)
    _playing_at(now)
    dj_triggers.clock_tick(bus, now=now)
    bus._enqueue("bed_play", {"url": "http://x/rain", "volume": 0.4})  # sleep engine
    engine["scheduled"][0][1]()
    assert _cmds("bed_stop") == []


def test_chime_needs_an_active_pool_and_music_playing(engine):
    _speech(engine)
    pool = _pool(active=False)
    now = _at(11, 0, 2)
    _playing_at(now)
    assert dj_triggers.clock_tick(bus, now=now)["reason"] == "pool inactive"
    pool.state["active"] = True
    bus.set_state(_state(10, playing=False))
    st, _ = bus._states["phone"]
    bus._states["phone"] = (st, now)
    assert dj_triggers.clock_tick(bus, now=now)["reason"] == "not playing"
    _playing_at(now, track=-1)  # a stream is not the DJ's music
    assert dj_triggers.clock_tick(bus, now=now)["reason"] == "not playing"
    assert _cmds("bed_play") == []
    _playing_at(now + 5)  # music starts inside the window: still chimes
    assert dj_triggers.clock_tick(bus, now=now + 5)["chime"] == "fired"


def test_chime_dropped_when_talking(engine):
    _speech(engine, talking=True)
    _pool()
    now = _at(14, 15, 2)
    _playing_at(now)
    res = dj_triggers.clock_tick(bus, now=now)
    assert (res["chime"], res["reason"]) == ("dropped", "talking")
    _speech(engine, talking=False)
    assert dj_triggers.clock_tick(bus, now=now + 5)["chime"] == "none"  # dropped, not delayed
    assert _cmds("bed_play") == []


def test_chime_dropped_when_more_than_20s_late(engine):
    _speech(engine)
    _pool()
    now = _at(14, 30, 25)
    _playing_at(now)
    res = dj_triggers.clock_tick(bus, now=now)
    assert (res["chime"], res["reason"]) == ("dropped", "late")
    assert _cmds("bed_play") == []


def test_chime_skipped_when_the_bed_layer_is_busy(engine):
    _speech(engine)
    _pool()
    bus._enqueue("bed_play", {"url": "http://x/rain", "volume": 0.4})  # sleep engine running
    now = _at(22, 0, 1)
    _playing_at(now)
    res = dj_triggers.clock_tick(bus, now=now)
    assert (res["chime"], res["reason"]) == ("skipped", "bed busy")
    assert len(_cmds("bed_play")) == 1


def test_chime_volume_setting_is_honored(engine, client):
    _speech(engine)
    _pool()
    r = client.patch("/api/playback/pool/chimes", json={"volume": 0.3})
    assert r.status_code == 200 and r.json()["volume"] == pytest.approx(0.3)
    now = _at(8, 15, 0)
    _playing_at(now)
    dj_triggers.clock_tick(bus, now=now)
    assert _cmds("bed_play")[0].payload["volume"] == pytest.approx(0.3)


def test_chimes_disabled_setting(engine):
    _speech(engine)
    pool = _pool()
    dj_triggers.set_chime_settings(pool, enabled=False)
    now = _at(8, 45, 0)
    _playing_at(now)
    assert dj_triggers.clock_tick(bus, now=now)["reason"] == "disabled"
    pool.stop()  # the preference survives a pool stop
    assert dj_triggers.chime_settings(pool)["enabled"] is False


def test_unknown_type_ack_disables_chimes_loudly(engine, client, capsys, caplog):
    _speech(engine)
    _pool()
    now = _at(16, 15, 1)
    _playing_at(now)
    dj_triggers.clock_tick(bus, now=now)
    rec = _cmds("bed_play")[0]
    bus.ack(rec.id, "unknown_type", "bed_play")
    with caplog.at_level("ERROR", logger="audiplex.dj_triggers"):
        dj_triggers.clock_tick(bus, now=now + 5)
    assert "CHIMES UNSUPPORTED" in capsys.readouterr().out
    assert "CHIMES UNSUPPORTED" in caplog.text
    status = client.get("/api/playback/pool").json()
    assert status["chimes_unsupported"] is True
    later = _at(16, 30, 1)
    _playing_at(later)
    assert dj_triggers.clock_tick(bus, now=later)["reason"] == "unsupported"
    assert len(_cmds("bed_play")) == 1


def test_hour_strike_variant_when_enabled(engine, tmp_path, monkeypatch):
    _speech(engine)
    monkeypatch.setenv("AUDIPLEX_CHIME_CACHE_DIR", str(tmp_path / "gen"))
    pool = _pool()
    dj_triggers.set_chime_settings(pool, hour_strikes=True)
    now = _at(15, 0, 2)
    _playing_at(now)
    dj_triggers.clock_tick(bus, now=now)
    assert _cmds("bed_play")[0].payload["url"].endswith("/api/dj/chimes/hour_03")
    assert (tmp_path / "gen" / "hour_03.wav").is_file()


def test_phone_poll_teaches_the_chime_base_url(engine, client, monkeypatch):
    from audiplex.routers import playback as playback_router

    monkeypatch.setattr(dj_triggers, "_client_base_url", None)
    monkeypatch.setattr(playback_router, "LONGPOLL_TIMEOUT_SECONDS", 0.01)
    assert dj_triggers.chime_base_url() is None
    client.get("/api/playback/command/next")
    assert dj_triggers.chime_base_url() == "http://testserver"


def test_chime_route_serves_the_committed_clip(client):
    r = client.get("/api/dj/chimes/q45")
    assert r.status_code == 200 and r.content[:4] == b"RIFF"
    assert client.get("/api/dj/chimes/../../etc").status_code == 404
    assert client.get("/api/dj/chimes/q20").status_code == 404


def test_chime_assets_are_self_synthesized_and_licensed():
    d = dj_triggers.chime_dir()
    manifest = json.loads((d / "manifest.json").read_text())
    assert set(manifest) == {"q00", "q15", "q30", "q45"}
    for entry in manifest.values():
        assert (d / entry["file"]).is_file()
        assert entry["total_seconds"] > entry["sound_seconds"]  # the loop-guard pad
    assert "self-synthesized" in (d / "LICENSE.txt").read_text()


def test_clock_cue_fires_once_at_its_minute(engine):
    _speech(engine)
    pool = _pool()
    pool.state["pending_cues"] = [
        {"id": 50, "trigger": {"kind": "clock", "minutes": [20]},
         "actions": [{"type": "insert_clip", "clip_id": 321}], "status": "pending",
         "done": False, "held_boundaries": 0},
    ]
    now = _at(10, 20, 5)
    res = dj_triggers.clock_tick(bus, now=now)
    assert res["cues"] == [50]
    assert _cmds("announce")[0].payload["mode"] == "next"
    dj_triggers.clock_tick(bus, now=now + 5)
    assert len(_cmds("announce")) == 1


# ----- ride-end outro (#5515) -----


def test_outro_arms_on_the_current_track_and_pauses_after_the_clip(engine, client):
    _speech(engine)
    bus.set_state(_state(20, after=[21]))
    r = client.post("/api/playback/pool/outro", json={"clip_id": 555, "agent": "Jarvis",
                                                      "duration_seconds": 4.0})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["armed"] is True and body["track_id"] == 20 and body["status"] == "fired"
    ann = _cmds("announce")
    assert len(ann) == 1 and ann[0].payload["mode"] == "next"  # after song 20 ends
    title = ann[0].payload["title"]
    assert title == "Ride outro \u00b7 Jarvis"
    assert _cmds("pause") == []  # never mid-song

    bus.set_state(_state(-3, title=title, position_ms=1000, duration_ms=4000))  # the clip plays
    (delay, fn), = engine["scheduled"]
    assert delay == pytest.approx(3.0 - dj_triggers.PAUSE_LEAD_S)
    assert _cmds("pause") == []
    fn()
    assert len(_cmds("pause")) == 1
    bus.set_state(_state(21, playing=False))
    bus.set_state(_state(21))  # resumed later: no second pause
    assert len(_cmds("pause")) == 1
    assert dj_pool.get_pool().state["outro"]["pause_state"] == "paused"


def test_outro_pauses_at_once_if_the_next_song_started_first(engine, client):
    _speech(engine)
    bus.set_state(_state(20, after=[21]))
    client.post("/api/playback/pool/outro", json={"clip_id": 556})
    bus.set_state(_state(21))  # the clip never showed in a report
    assert len(_cmds("pause")) == 1
    assert dj_pool.get_pool().state["outro"]["pause_state"] == "paused_fallback"


def test_outro_held_while_talking_fires_at_the_next_boundary(engine, client):
    _speech(engine, talking=True)
    bus.set_state(_state(20, after=[21]))
    body = client.post("/api/playback/pool/outro", json={"clip_id": 557}).json()
    assert body["status"] == "held" and _cmds("announce") == []
    _speech(engine, talking=False)
    bus.set_state(_state(21))
    assert len(_cmds("announce")) == 1


def test_dropped_outro_still_stops_the_music_at_the_boundary(engine, client):
    _speech(engine, talking=True, spoke=_iso(time.time() - 3600))
    bus.set_state(_state(20, after=[21]))
    assert client.post("/api/playback/pool/outro", json={"clip_id": 560}).json()["status"] == "held"
    # He kept talking, so his last_spoke_at moved past the render: the words are stale.
    _speech(engine, talking=False, spoke=_iso(time.time() + 5))
    bus.set_state(_state(21))
    outro = dj_pool.get_pool().state["outro"]
    assert outro["drop_reason"] == "stale" and outro["pause_state"] == "paused_no_outro"
    assert _cmds("announce") == [] and len(_cmds("pause")) == 1


def test_outro_refused_when_nothing_is_playing(engine, client):
    r = client.post("/api/playback/pool/outro", json={"clip_id": 558})
    assert r.status_code == 409 and r.json()["armed"] is False
    bus.set_state(_state(-1, title="Radio Free Luna"))
    assert client.post("/api/playback/pool/outro", json={"clip_id": 558}).status_code == 409


def test_outro_say_is_rendered_server_side(engine, client, monkeypatch):
    _speech(engine)
    bus.set_state(_state(20))

    async def fake_render(text, title):
        return {"clip_id": 777, "duration_seconds": 3.5, "voice": "v", "rendered_at": time.time()}

    monkeypatch.setattr(dj_triggers, "render_say", fake_render)
    body = client.post("/api/playback/pool/outro",
                       json={"say": "That's the ride.", "agent": "Karen"}).json()
    assert body["armed"] is True and body["clip_id"] == 777
    assert _cmds("announce")[0].payload["duration_seconds"] == 3.5
    status = client.get("/api/playback/pool").json()
    assert status["outro"]["say"] == "That's the ride."
    assert status["outro"]["agent"] == "Karen"


def test_outro_render_failure_is_503_and_arms_nothing(engine, client, monkeypatch):
    bus.set_state(_state(20))

    async def broken(text, title):
        raise RuntimeError("No TTS backend configured")

    monkeypatch.setattr(dj_triggers, "render_say", broken)
    r = client.post("/api/playback/pool/outro", json={"say": "bye"})
    assert r.status_code == 503
    assert _cmds("announce") == [] and dj_pool.get_pool().state.get("outro") is None


def test_outro_disarm(engine, client):
    _speech(engine, talking=True)
    bus.set_state(_state(20, after=[21]))
    client.post("/api/playback/pool/outro", json={"clip_id": 559})
    assert client.delete("/api/playback/pool/outro").json() == {"disarmed": True}
    _speech(engine, talking=False)
    bus.set_state(_state(21))
    assert _cmds("announce") == []


# ----- status -----


def test_pool_status_shows_cues_chimes_and_outro(engine, client):
    _speech(engine, talking=True)
    pool = _pool()
    pool.state["pending_cues"] = [_cue(60, "track_end", 20)]
    bus.set_state(_state(20))  # held
    body = client.get("/api/playback/pool").json()
    assert body["cues"] == [{
        "id": 60, "trigger": {"kind": "track_end", "track_id": 20}, "say": "patter 60",
        "status": "held", "held_boundaries": 1, "clip_id": 900, "agent": None,
    }]
    assert body["chimes"]["enabled"] is True and body["chimes"]["volume"] == pytest.approx(0.12)
    assert body["chimes_unsupported"] is False and body["outro"] is None


def test_mcp_pool_status_renders_cues_chimes_outro(monkeypatch):
    async def fake_get(path):
        return {
            "active": True, "spec_id": 1, "balance_mode": "even", "ahead": 4,
            "eligible_count": 3, "played_this_session_count": 0, "lanes": [],
            "cues": [{"id": 1, "trigger": {"kind": "track_end", "track_id": 9},
                      "say": "hello", "status": "held", "held_boundaries": 1}],
            "chimes": {"enabled": True, "volume": 0.12, "hour_strikes": False},
            "chimes_unsupported": True,
            "outro": {"track_id": 9, "status": "fired", "pause_state": "armed", "say": "bye"},
        }

    monkeypatch.setattr(mcp_server, "_get", fake_get)
    out = asyncio.run(mcp_server.dj_pool_status())
    assert "[held, held 1x] track_end 9: hello" in out
    assert "UNSUPPORTED" in out
    assert "Outro armed: after track 9" in out


# ----- cue clip render (#5480) -----


def test_spec_note_renders_say_ahead_of_time(monkeypatch):
    posts = []

    async def fake_render(text, title):
        assert text == "Up next, a slow one."
        return {"clip_id": 4242, "url": "/api/dj/clips/4242", "duration_seconds": 2.5}

    async def fake_post(path, body):
        posts.append((path, body))
        return {"id": 1}

    monkeypatch.setattr(mcp_server, "_render_clip", fake_render)
    monkeypatch.setattr(mcp_server, "_post", fake_post)
    out = asyncio.run(mcp_server.dj_spec_note("ride", after_track_id=5, say="Up next, a slow one.",
                                              agent="Jarvis"))
    assert "pre-rendered" in out
    (path, body), = posts
    assert body["clip_id"] == 4242 and body["clip_title"] == "DJ break \u00b7 Jarvis"
    assert body["agent"] == "Jarvis" and body["clip_duration"] == 2.5
    assert dj_triggers._parse_ts(body["rendered_at"]) == pytest.approx(time.time(), abs=60)


def test_spec_note_render_failure_adds_no_cue(monkeypatch):
    async def fake_render(text, title):
        return "TTS is not configured: nope"

    async def fake_post(path, body):
        raise AssertionError("no cue without its clip")

    monkeypatch.setattr(mcp_server, "_render_clip", fake_render)
    monkeypatch.setattr(mcp_server, "_post", fake_post)
    out = asyncio.run(mcp_server.dj_spec_note("ride", after_track_id=5, say="hi"))
    assert out.startswith("ERROR")


@pytest.fixture
def specs(db_engine):
    _migrate_create_dj_mix_specs(db_engine)


def test_spec_note_route_stores_the_clip_fields(client, specs):
    assert client.post("/api/playback/mix-specs", json={"name": "ride"}).status_code == 200
    note = client.post("/api/playback/mix-specs/ride/notes", json={
        "after_track_id": 5, "say": "hi", "clip_id": "4242", "clip_title": "DJ break \u00b7 Jarvis",
        "clip_duration": 2.5, "rendered_at": "2026-09-28T22:00:00Z", "agent": "Jarvis", "voice": "v",
    }).json()
    assert note["clip_id"] == 4242 and note["agent"] == "Jarvis"
    assert dj_triggers.is_engine_cue(note)


def test_create_spec_accepts_a_list_request_text(client, specs):
    r = client.post("/api/playback/mix-specs", json={
        "name": "todd-ride-mix", "request_text": ["play the ride mix", "and some faster ones"],
    })
    assert r.status_code == 200, r.text
    got = client.get("/api/playback/mix-specs/todd-ride-mix").json()
    assert json.loads(got["request_text"]) == ["play the ride mix", "and some faster ones"]


# ----- validation + geofence matcher -----


def test_validate_trigger_and_actions():
    assert dj_triggers.validate_trigger({"kind": "track_end", "track_id": "7"}) == {
        "kind": "track_end", "track_id": 7}
    assert dj_triggers.validate_trigger({"kind": "clock", "minutes": [45, 15]})["minutes"] == [15, 45]
    assert dj_triggers.validate_trigger({"kind": "clock", "at": "7:05"})["at"] == "07:05"
    for bad in ({"kind": "nope"}, {"kind": "track_start", "track_id": -1},
                {"kind": "clock", "minutes": [60]}, {"kind": "geofence", "lat": 1, "lon": 2, "radius_m": 0}):
        with pytest.raises(ValueError):
            dj_triggers.validate_trigger(bad)
    with pytest.raises(ValueError):
        dj_triggers.validate_actions([{"type": "pause"}])  # never a bare mid-song pause
    ok = dj_triggers.validate_actions([{"type": "insert_clip", "clip_id": "5"}, {"type": "pause"}])
    assert ok == [{"type": "insert_clip", "clip_id": 5}, {"type": "pause"}]


def test_geofence_matcher():
    home = dj_triggers.validate_trigger(
        {"kind": "geofence", "lat": 45.5231, "lon": -122.6765, "radius_m": 150, "on": "enter"})
    near = (45.5236, -122.6770)   # ~70 m away
    far = (45.5331, -122.6765)    # ~1.1 km away
    assert 50 < dj_triggers.haversine_m(home["lat"], home["lon"], *near) < 100
    assert dj_triggers.in_geofence(home, *near) and not dj_triggers.in_geofence(home, *far)
    assert dj_triggers.match_geofence(home, False, *near) is True
    assert dj_triggers.match_geofence(home, True, *near) is False   # already inside
    assert dj_triggers.match_geofence(home, None, *near) is False   # unknown is not a transition
    leave = {**home, "on": "exit"}
    assert dj_triggers.match_geofence(leave, True, *far) is True
    assert dj_triggers.match_geofence({**home, "on": "inside"}, None, *near) is True


def test_clock_slot():
    t = datetime(2026, 9, 28, 10, 44, 30)
    assert dj_triggers.clock_slot({"minutes": [0, 15, 30, 45]}, t) == datetime(2026, 9, 28, 10, 30)
    assert dj_triggers.clock_slot({"at": "11:00"}, t) == datetime(2026, 9, 27, 11, 0)
    assert dj_triggers.clock_slot({"at": "10:00"}, t) == datetime(2026, 9, 28, 10, 0)


# ----- #5544: staleness only for REACTIVE cues -----


def test_planned_cue_ignores_staleness_and_fires(engine):
    rendered = time.time() - 60
    _speech(engine, spoke=_iso(rendered + 30))  # he spoke after it was rendered
    pool = _pool()
    pool.state["pending_cues"] = [_cue(40, "track_end", 10, rendered_at=rendered, planned=True)]
    bus.set_state(_state(10))
    assert len(_cmds("announce")) == 1
    assert pool.state["pending_cues"][0]["status"] == "fired"


def test_old_render_is_planned_and_not_stale(engine):
    rendered = time.time() - dj_triggers.REACTIVE_WINDOW_S - 120
    _speech(engine, spoke=_iso(rendered + 60))
    pool = _pool()
    pool.state["pending_cues"] = [_cue(41, "track_end", 10, rendered_at=rendered)]
    bus.set_state(_state(10))
    assert len(_cmds("announce")) == 1
    assert pool.state["pending_cues"][0]["reactive"] is False


def test_planned_cue_still_held_then_dropped_after_two_boundaries(engine):
    _speech(engine, talking=True)
    pool = _pool()
    pool.state["pending_cues"] = [_cue(42, "track_end", 10, planned=True)]
    bus.set_state(_state(10, after=[11, 12]))
    assert pool.state["pending_cues"][0]["status"] == "held"
    bus.set_state(_state(11, after=[12]))
    cue = pool.state["pending_cues"][0]
    assert cue["status"] == "dropped" and cue["drop_reason"] == "held_too_long"
    assert _cmds("announce") == []


def test_reactive_classification_is_frozen_at_first_check(engine):
    rendered = time.time() - 300  # reactive at its first check
    _speech(engine, talking=True)
    pool = _pool()
    cue = _cue(43, "track_end", 10, rendered_at=rendered)
    assert dj_triggers.hold_guard(cue) == "held" and cue["reactive"] is True
    # Much later he has spoken since the render: aging past the window must not rescue it.
    later = {"talking": False, "last_spoke_at": rendered + 30, "readable": True}
    cue["rendered_at"] = rendered
    assert dj_triggers.is_reactive(cue, now=rendered + 10_000) is True
    assert dj_triggers.hold_guard(cue, later) == "dropped" and cue["drop_reason"] == "stale"


def test_spec_cues_load_as_planned(client, specs):
    assert client.post("/api/playback/mix-specs", json={"name": "ride2"}).status_code == 200
    client.post("/api/playback/mix-specs/ride2/notes", json={
        "after_track_id": 5, "say": "hi", "clip_id": "4243", "clip_title": "DJ break",
        "clip_duration": 2.5, "rendered_at": "2026-09-28T22:00:00Z",
    })
    spec = client.get("/api/playback/mix-specs/ride2").json()
    r = client.post("/api/playback/pool", json={"spec_id": spec["id"], "lanes": {"a": [1, 2]}})
    assert r.status_code == 200, r.text
    cues = dj_pool.get_pool().state["pending_cues"]
    assert cues and all(c.get("planned") is True for c in cues)


def test_planned_cue_waits_for_the_floor_while_todd_is_speaking(engine):
    # #5546: planned is exempt from STALENESS only, never from the floor.
    rendered = time.time() - 3600  # planned, and he spoke long after the render
    _speech(engine, talking=True, spoke=_iso(time.time()))  # STT shows him mid-sentence
    pool = _pool()
    pool.state["pending_cues"] = [_cue(44, "track_end", 10, rendered_at=rendered, planned=True)]
    bus.set_state(_state(10, after=[11]))
    cue = pool.state["pending_cues"][0]
    assert cue["status"] == "held" and not cue.get("done")  # deferred, not dropped
    assert _cmds("announce") == []  # not played over him
    _speech(engine, talking=False, spoke=_iso(time.time()))  # he finished
    bus.set_state(_state(11))
    assert len(_cmds("announce")) == 1 and cue["status"] == "fired"
