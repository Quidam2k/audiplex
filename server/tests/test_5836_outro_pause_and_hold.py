"""#5836 the after-outro pause retries in 3 s and expires 10 s into the next song; a resume or newer
playback command cancels it. #5837 a DJ cue never starts while a persona line (agent_audio_until) is on air.

Evidence (11/10 22:05 UTC): the outro pause was delivered, its long-poll reply never reached the phone,
and the bus's 60 s redelivery paused Todd a minute into the next song.
"""

import json
import time
from datetime import datetime, timezone

import pytest

from audiplex import dj_pool, dj_triggers, playback_bus
from audiplex.playback_bus import bus


@pytest.fixture(autouse=True)
def engine(tmp_path, monkeypatch):  # #5836 same isolation as test_dj_triggers
    speech = tmp_path / "speech_state.json"
    monkeypatch.setenv("DJ_SPEECH_STATE_FILE", str(speech))
    monkeypatch.delenv("AUDIPLEX_PUBLIC_URL", raising=False)
    monkeypatch.setattr(dj_triggers, "_client_base_url", "http://phone-sees-this:8100")
    monkeypatch.setattr(playback_bus, "_pool_session", lambda: None)
    scheduled = []
    monkeypatch.setattr(dj_triggers, "_schedule", lambda delay, fn: scheduled.append((delay, fn)))
    diag = []
    real = playback_bus._append_diag
    monkeypatch.setattr(playback_bus, "_append_diag", lambda kind, rec: (diag.append(kind), real(kind, rec)))
    dj_triggers.reset_warnings()
    bus.reset()
    yield {"speech": speech, "scheduled": scheduled, "diag": diag}
    bus.reset()


def _iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _speech(engine, talking=False, agent_until=None):
    data = {"stt_active": talking, "talk_active": False, "composing": False, "todd_last_spoke_at": None}
    if agent_until is not None:
        data["agent_audio_until"] = agent_until
    engine["speech"].write_text(json.dumps(data), encoding="utf-8")


def _state(current, after=(), playing=True, title="song", position_ms=0, duration_ms=0):
    queue = [{"index": 0, "id": current, "title": title}] + [
        {"index": i + 1, "id": t, "title": f"t{t}"} for i, t in enumerate(after)]
    return {"playing": playing, "track": {"id": current, "title": title}, "queue_index": 0,
            "queue": queue, "position_ms": position_ms, "duration_ms": duration_ms}


def _cmds(kind=None):
    return [r for r in bus._commands.values() if kind is None or r.type == kind]


def _cue(cid, track_id):
    return {"id": cid, "trigger": {"kind": "track_end", "track_id": track_id}, "clip_id": 900,
            "clip_title": f"DJ break #{cid}", "clip_duration": 5.0, "rendered_at": time.time(),
            "planned": True, "status": "pending", "done": False, "held_boundaries": 0}


# ----- bus: per-command redelivery, expiry, supersede (#5836) -----


def test_fast_retry_command_is_reoffered_at_3s_others_wait_60():  # #5836
    fast = bus._enqueue("pause", {}, redeliver_after_s=3.0)
    t0 = 1000.0
    assert bus._claim(t0, "phone") is fast
    assert bus._claim(t0 + 2.9, "phone") is None
    assert bus._next_redelivery_in(t0 + 2.0) == pytest.approx(1.0)
    again = bus._claim(t0 + 3.0, "phone")
    assert again is fast and fast.delivery_count == 2
    bus.ack(fast.id, "ok")
    plain = bus._enqueue("skip", {})
    assert bus._claim(t0, "phone") is plain
    assert bus._claim(t0 + 59.0, "phone") is None
    assert bus._claim(t0 + 60.0, "phone") is plain


def test_expired_command_is_retired_never_delivered_and_logged(engine):  # #5836
    rec = bus._enqueue("pause", {}, redeliver_after_s=3.0, expires_at=1010.0)
    assert bus._claim(1000.0, "phone") is rec
    assert bus._claim(1010.0, "phone") is None
    assert rec.status == playback_bus.STATUS_FAILED and rec.ack_status == "expired"
    assert "cmd_expired" in engine["diag"]
    assert bus._claim(1100.0, "phone") is None
    bus.ack(rec.id, "ok")  # a late ack from the phone is harmless
    assert bus._claim(1200.0, "phone") is None


@pytest.mark.parametrize("newer", ["resume", "play_now", "pause", "skip"])
@pytest.mark.parametrize("delivered", [False, True])
def test_newer_playback_command_supersedes_an_owed_outro_pause(engine, newer, delivered):  # #5836
    rec = bus._enqueue("pause", {}, redeliver_after_s=3.0, supersede_on_newer=True)
    if delivered:
        bus._claim(1000.0, "phone")
    bus._enqueue(newer, {"track_ids": [1]} if newer == "play_now" else {})
    assert rec.ack_status == "superseded"
    assert "cmd_superseded" in engine["diag"]
    assert all(r is not rec for r in [bus._claim(2000.0, "phone"), bus._claim(2001.0, "phone")])


@pytest.mark.parametrize("newer", ["queue", "play_next", "announce", "bed_play", "bed_stop"])
def test_queue_edits_and_beds_do_not_supersede(newer):  # #5836 the pool tops up at every boundary
    rec = bus._enqueue("pause", {}, redeliver_after_s=3.0, supersede_on_newer=True)
    bus._enqueue(newer, {"track_ids": [5]} if newer in ("queue", "play_next") else {"url": "x"})
    assert rec.status == playback_bus.STATUS_QUEUED and rec.ack_status is None


def test_resume_after_a_real_pause_supersedes(monkeypatch):  # #5836 Todd pressed play: never re-pause him
    clock = [1000.0]
    monkeypatch.setattr(playback_bus.time, "time", lambda: clock[0])
    rec = bus._enqueue("pause", {}, redeliver_after_s=3.0, supersede_on_newer=True)
    bus._claim(clock[0], "phone")
    bus.set_state(_state(21, playing=False))
    clock[0] += 5.0
    bus.set_state(_state(21, playing=True))
    assert rec.ack_status == "superseded"


def test_a_sub_second_gap_between_items_is_not_a_resume(monkeypatch):  # #5836
    clock = [1000.0]
    monkeypatch.setattr(playback_bus.time, "time", lambda: clock[0])
    rec = bus._enqueue("pause", {}, redeliver_after_s=3.0, supersede_on_newer=True)
    bus.set_state(_state(-3, playing=True, title="clip"))
    clock[0] += 0.5
    bus.set_state(_state(21, playing=False))
    clock[0] += 0.2
    bus.set_state(_state(21, playing=True))
    clock[0] += 1.0
    bus.set_state(_state(22, playing=True))  # playing -> playing is never a resume either
    assert rec.status == playback_bus.STATUS_QUEUED


# ----- triggers: the outro pause uses the fast, expiring policy (#5836) -----


def test_outro_pause_retries_fast_expires_and_is_recorded(engine, client):  # #5836
    _speech(engine)
    bus.set_state(_state(20, after=[21]))
    client.post("/api/playback/pool/outro", json={"clip_id": 555, "duration_seconds": 4.0})
    title = _cmds("announce")[0].payload["title"]
    bus.set_state(_state(-3, title=title, position_ms=1000, duration_ms=4000))
    (_delay, fn), = engine["scheduled"]
    before = time.time()
    fn()
    pause, = _cmds("pause")
    assert pause.redeliver_after_s == dj_triggers.OUTRO_PAUSE_RETRY_S
    assert pause.supersede_on_newer is True
    expected = before + dj_triggers.PAUSE_LEAD_S + dj_triggers.OUTRO_PAUSE_EXPIRE_S
    assert pause.expires_at == pytest.approx(expected, abs=2.0)
    assert dj_pool.get_pool().state["outro"]["pause_command_id"] == pause.id


# ----- hold guard on agent_audio_until (#5837) -----


@pytest.mark.parametrize("until,held", [
    (lambda now: _iso(now + 5), True),
    (lambda now: None, False),
    (lambda now: "garbage", False),
    (lambda now: _iso(now - 5), False),
    (lambda now: _iso(now + 600), False),  # bogus-far: fail-open
])
def test_hold_guard_on_agent_audio_until(engine, until, held):  # #5837
    _speech(engine, agent_until=until(time.time()))
    cue = _cue(1, 10)
    assert dj_triggers.hold_guard(cue) == ("held" if held else "fire")
    if held:
        assert cue["held_reason"] == "agent_audio"


def test_agent_audio_hold_keeps_the_boundary_cap(engine):  # #5837
    _speech(engine, agent_until=_iso(time.time() + 30))
    cue = _cue(1, 10)
    results = [dj_triggers.hold_guard(cue) for _ in range(dj_triggers.MAX_HELD_BOUNDARIES)]
    assert results[-1] == "dropped" and cue["drop_reason"] == "held_too_long"


def test_cue_held_over_a_persona_line_fires_at_the_next_boundary(engine):  # #5837
    _speech(engine, agent_until=_iso(time.time() + 20))
    pool = dj_pool.get_pool()
    pool.state["active"] = True
    pool.state["pending_cues"] = [_cue(1, 10)]
    bus.set_state(_state(10, after=[11]))
    assert _cmds("announce") == []
    _speech(engine)
    bus.set_state(_state(11, after=[12]))
    assert len(_cmds("announce")) == 1


# ----- held outro re-check: one outro, never two (#5837) -----


def _arm_held_outro(engine, client):
    _speech(engine, agent_until=_iso(time.time() + 20))
    bus.set_state(_state(20, after=[21, 22]))
    body = client.post("/api/playback/pool/outro", json={"clip_id": 557}).json()
    assert body["status"] == "held" and _cmds("announce") == []
    (delay, fn), = engine["scheduled"]
    assert delay == dj_triggers.OUTRO_RECHECK_S
    engine["scheduled"].clear()
    return fn


def test_held_outro_fires_once_when_the_line_ends(engine, client):  # #5837
    fn = _arm_held_outro(engine, client)
    assert dj_pool.get_pool().state["outro"].get("recheck_until")
    bus.set_state(_state(21, after=[22]))  # a boundary while the timer owns it: no fire
    assert _cmds("announce") == []
    _speech(engine)
    fn()
    assert len(_cmds("announce")) == 1
    fn()
    bus.set_state(_state(22))
    assert len(_cmds("announce")) == 1


def test_held_outro_reschedules_then_fires_at_the_cap(engine, client, monkeypatch):  # #5837
    fn = _arm_held_outro(engine, client)
    fn()  # still on air: reschedules
    (delay, fn2), = engine["scheduled"]
    assert delay == dj_triggers.OUTRO_RECHECK_S and _cmds("announce") == []
    _speech(engine, agent_until=_iso(time.time() + 100))
    future = time.time() + dj_triggers.OUTRO_RECHECK_CAP_S + 1
    monkeypatch.setattr(dj_triggers.time, "time", lambda: future)
    fn2()
    assert len(_cmds("announce")) == 1


def test_talking_during_recheck_hands_it_back_to_the_boundary(engine, client):  # #5837
    fn = _arm_held_outro(engine, client)
    _speech(engine, talking=True)
    fn()
    outro = dj_pool.get_pool().state["outro"]
    assert engine["scheduled"] == [] and "recheck_until" not in outro and outro["held_reason"] == "talking"
    _speech(engine)
    bus.set_state(_state(21, after=[22]))
    assert len(_cmds("announce")) == 1


def test_disarm_during_recheck_fires_nothing(engine, client):  # #5837
    fn = _arm_held_outro(engine, client)
    dj_triggers.disarm_outro()
    _speech(engine)
    fn()
    assert _cmds("announce") == []


def test_resume_counts_from_the_pause_start_not_the_last_paused_report(monkeypatch):  # #5836 review fix
    clock = [1000.0]
    monkeypatch.setattr(playback_bus.time, "time", lambda: clock[0])
    rec = bus._enqueue("pause", {}, redeliver_after_s=3.0, supersede_on_newer=True)
    bus.set_state(_state(21, playing=True, position_ms=1000))
    clock[0] += 1.0
    bus.set_state(_state(21, playing=False, position_ms=2000))
    for i in range(10):  # a chatty reporter: paused heartbeats every 0.5 s
        clock[0] += 0.5
        bus.set_state(_state(21, playing=False, position_ms=2000 + i))
    clock[0] += 0.2
    bus.set_state(_state(21, playing=True, position_ms=2100))
    assert rec.ack_status == "superseded"


def test_recheck_hands_a_stale_outro_back_to_the_boundary_drop(engine, client):  # #5837 review fix
    fn = _arm_held_outro(engine, client)
    data = json.loads(engine["speech"].read_text(encoding="utf-8"))
    data["todd_last_spoke_at"] = _iso(time.time() + 5)  # he spoke after the render: the words are stale
    data.pop("agent_audio_until", None)
    engine["speech"].write_text(json.dumps(data), encoding="utf-8")
    fn()
    outro = dj_pool.get_pool().state["outro"]
    assert _cmds("announce") == [] and "recheck_until" not in outro and outro["held_reason"] == "stale"
    bus.set_state(_state(21, after=[22]))
    outro = dj_pool.get_pool().state["outro"]
    assert outro["drop_reason"] == "stale" and outro["pause_state"] == "paused_no_outro"
    assert _cmds("announce") == []


def test_boundary_never_fires_an_outro_the_timer_already_fired(engine, client):  # #5837 review fix
    fn = _arm_held_outro(engine, client)
    _speech(engine)
    fn()
    outro = dj_pool.get_pool().state["outro"]
    outro["status"] = "held"  # even if a stale status lingered, done wins
    bus.set_state(_state(21, after=[22]))
    assert len(_cmds("announce")) == 1
