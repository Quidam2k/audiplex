"""#5648 ahead-of-time DJ breaks (Todd msg 59058, asg 11715): render early, retry
with time to spare, saved clips only as the capped exception, pause on failure.

In-process bus + pool, a temp speech-state file, a fake render, and a spawner that
holds each render try until the test runs it. Nothing reaches Athena or a phone.
"""

import asyncio
import json
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from audiplex import dj_pool, dj_prepare, dj_triggers, playback_bus
from audiplex.database import Base
from audiplex.models import DjClipUse
from audiplex.playback_bus import bus

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from audiplex_mcp import server as mcp_server  # noqa: E402

CUR, NXT = 11, 12


class Dummy:
    def __init__(self, coro):
        self.coro, self.cancelled = coro, False

    def done(self):
        return False

    def cancel(self):
        self.cancelled = True


class Spawner:
    def __init__(self):
        self.pending: list[Dummy] = []

    def __call__(self, coro):
        d = Dummy(coro)
        self.pending.append(d)
        return d

    def run_all(self):
        while self.pending:
            d = self.pending.pop(0)
            if d.cancelled:
                d.coro.close()
            else:
                asyncio.run(d.coro)


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    speech = tmp_path / "speech_state.json"
    monkeypatch.setenv("DJ_SPEECH_STATE_FILE", str(speech))
    monkeypatch.setenv("AUDIPLEX_DJ_PREPARE_LOG", str(tmp_path / "log.jsonl"))
    monkeypatch.setenv("AUDIPLEX_PANTHEON_DB", str(tmp_path / "pantheon.db"))  # missing = fail-open
    monkeypatch.setattr(playback_bus, "_pool_session", lambda: None)
    monkeypatch.setattr(dj_triggers, "_schedule", lambda delay, fn: None)
    monkeypatch.setattr(dj_prepare, "_bank_clip", lambda entry: None)
    # set_state feeds on_state at wall-clock time; the tests drive boundaries themselves, at their now
    monkeypatch.setattr(bus, "_maybe_run_triggers", lambda state, device_key: None)
    dj_triggers.reset_warnings()
    dj_prepare._TASKS.clear()
    bus.reset()
    pool = dj_pool.get_pool()
    pool.state["prepared"] = []
    pool.state["outro"] = None
    pool.state.pop("prepare_settings", None)
    speech.write_text(json.dumps({"stt_active": False}), encoding="utf-8")
    yield {"speech": speech, "log": tmp_path / "log.jsonl", "pdb": tmp_path / "pantheon.db",
           "pool": pool, "spawn": Spawner()}
    bus.reset()


def _speech(env, **kw):
    data = {"stt_active": False, "talk_active": False, "composing": False, **kw}
    env["speech"].write_text(json.dumps(data), encoding="utf-8")


def _play(cur=CUR, nxt=NXT, pos_s=15.0, dur_s=200.0) -> float:
    """Report a playing song; returns the report time (its end is t + dur - pos)."""
    bus.set_state({
        "playing": True,
        "track": {"id": cur, "title": f"t{cur}"},
        "queue_index": 0,
        "queue": [{"index": 0, "id": cur}, {"index": 1, "id": nxt}],
        "position_ms": int(pos_s * 1000),
        "duration_ms": int(dur_s * 1000),
    })
    return bus.get_state()["updated_at"]


def _render_ok(monkeypatch, clip_id=777):
    async def ok(text, title):
        return {"clip_id": clip_id, "duration_seconds": 4.0, "rendered_at": time.time()}
    monkeypatch.setattr(dj_prepare, "_render", ok)


def _render_fail(monkeypatch):
    calls = []

    async def fail(text, title):
        calls.append(text)
        raise RuntimeError("athena down")
    monkeypatch.setattr(dj_prepare, "_render", fail)
    return calls


def _cmds(kind):
    return [r for r in bus._commands.values() if r.type == kind]


def _entry(env):
    return env["pool"].state["prepared"][-1]


def _tick(env, now):
    out = dj_prepare.tick(bus, env["pool"], now=now, spawn=env["spawn"])
    env["spawn"].run_all()
    return out


def _walk(env, t, until, step=5.0):
    """Tick every step seconds; returns the now of each try start."""
    starts, now = [], t
    while now <= until:
        if _tick(env, now)["started"]:
            starts.append(now)
        now += step
    return starts


def test_prepare_renders_now_and_fires_near_end(env, monkeypatch):
    _render_ok(monkeypatch)
    t = _play()
    assert dj_prepare.prepare(bus, "That was X, here's Y", pool=env["pool"], now=t)["prepared"]
    _tick(env, t)
    assert _entry(env)["status"] == "ready" and _entry(env)["clip_id"] == 777
    _tick(env, t + 100)
    assert not _cmds("announce")
    _speech(env, todd_last_spoke_at=datetime.now(timezone.utc).isoformat())  # spoke after the render
    _tick(env, t + 172)  # ~13 s left
    ann = _cmds("announce")
    assert len(ann) == 1 and ann[0].payload["clip_id"] == 777 and ann[0].payload["mode"] == "next"
    assert _entry(env)["status"] == "fired"


def test_failed_render_retries_on_schedule_and_stops_by_T_minus_25(env, monkeypatch):
    calls = _render_fail(monkeypatch)
    t = _play()
    end = t + 185
    dj_prepare.prepare(bus, "line", pool=env["pool"], now=t)
    starts = _walk(env, t, t + 180)
    assert len(calls) == 4 and len(starts) == 4
    for got, due in zip(starts, (0, 30, 60, 120)):
        assert t + due <= got < t + due + 5
    assert max(starts) <= end - dj_prepare.LAST_TRY_LEAD_S
    assert _entry(env)["status"] == "pause_at_boundary"


def test_short_song_scales_retry_schedule(env, monkeypatch):
    due = dj_prepare.try_due_times(1000.0, 1090.0)
    assert due[0] == 1000.0 and due == sorted(due) and due[-1] <= 1090.0 - dj_prepare.LAST_TRY_LEAD_S
    calls = _render_fail(monkeypatch)
    t = _play(pos_s=110.0, dur_s=200.0)  # 90 s left
    dj_prepare.prepare(bus, "line", pool=env["pool"], now=t)
    starts = _walk(env, t, t + 88, step=2.0)
    assert len(calls) == 4
    assert max(starts) <= t + 90 - dj_prepare.LAST_TRY_LEAD_S


def test_next_track_mismatch_drops_and_pauses(env, monkeypatch):
    _render_ok(monkeypatch)
    t = _play()
    dj_prepare.prepare(bus, "next up is twelve", pool=env["pool"], now=t)
    _tick(env, t)
    assert _entry(env)["status"] == "ready"
    t2 = _play(nxt=99, pos_s=180.0)  # Todd reordered: 99 is next now; 20 s left
    _tick(env, t2 + 7)
    assert not _cmds("announce")
    assert _entry(env)["status"] == "pause_at_boundary"
    _play(cur=99, nxt=13, pos_s=0.5)
    dj_triggers.on_state(bus.get_state(), bus, env["pool"], now=t2 + 21)  # the song ended
    pauses = [r for r in _cmds("pause") if r.source == "dj_prepare:fallback_pause"]
    assert len(pauses) == 1 and _entry(env)["status"] == "paused"


def test_all_fail_empty_cache_pauses(env, monkeypatch):
    _render_fail(monkeypatch)
    t = _play()
    dj_prepare.prepare(bus, "line", pool=env["pool"], now=t)
    _walk(env, t, t + 175)
    assert _entry(env)["status"] == "pause_at_boundary" and not _cmds("announce")
    _play(cur=NXT, nxt=13, pos_s=0.5)
    dj_triggers.on_state(bus.get_state(), bus, env["pool"], now=t + 186)
    assert [r for r in _cmds("pause") if r.source == "dj_prepare:fallback_pause"]
    lines = [json.loads(x) for x in env["log"].read_text(encoding="utf-8").splitlines()]
    fb = [x for x in lines if x["event"] == "fallback"]
    assert fb and fb[0]["used"] == "pause" and "4 tries failed" in fb[0]["failed"]
    assert any(x["event"] == "fallback_pause" for x in lines)
    assert sum(x["event"] == "try_start" for x in lines) == 4  # paired with persona latency later


@pytest.fixture
def ledger(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(dj_prepare, "_session", lambda: Session())
    monkeypatch.setattr(dj_prepare, "_bank_clip", lambda entry: ("dj_next", "x.wav"))
    monkeypatch.setattr(dj_prepare, "_bank_voice", lambda: "chris")
    monkeypatch.setattr(dj_prepare, "_store_bank_clip", lambda path: (555, 3.0))
    return Session


def _failed_to_pause(env, monkeypatch):
    _render_fail(monkeypatch)
    t = _play()
    dj_prepare.prepare(bus, "line", pool=env["pool"], now=t)
    _walk(env, t, t + 175)
    assert _entry(env)["status"] == "pause_at_boundary"
    return t


def _fallback_pauses():
    return [r for r in _cmds("pause") if r.source == "dj_prepare:fallback_pause"]


def test_a_skip_never_pauses(env, monkeypatch):
    t = _failed_to_pause(env, monkeypatch)
    _play(cur=NXT, nxt=13, pos_s=0.5)
    dj_triggers.on_state(bus.get_state(), bus, env["pool"], now=t + 172)  # Todd skipped at T-13
    assert not _fallback_pauses() and _entry(env)["drop_reason"] == "skipped"


def test_pause_waits_out_a_dj_clip_then_pauses_on_the_next_song(env, monkeypatch):
    t = _failed_to_pause(env, monkeypatch)
    _play(cur=-5, nxt=NXT, pos_s=0.5, dur_s=4)
    dj_triggers.on_state(bus.get_state(), bus, env["pool"], now=t + 186)
    assert not _fallback_pauses() and _entry(env)["status"] == "pause_at_boundary"
    _play(cur=NXT, nxt=13, pos_s=0.5)
    dj_triggers.on_state(bus.get_state(), bus, env["pool"], now=t + 191)
    assert len(_fallback_pauses()) == 1


def test_pause_expires_when_playback_stopped(env, monkeypatch):
    t = _failed_to_pause(env, monkeypatch)
    _play(cur=40, nxt=41, pos_s=0.5)
    dj_triggers.on_state(bus.get_state(), bus, env["pool"], now=t + 185 + 600)  # an hour-ish later
    assert not _fallback_pauses() and _entry(env)["drop_reason"] == "pause_expired"


def test_pool_stop_cancels_inflight_tries(env, monkeypatch):
    _render_fail(monkeypatch)
    t = _play()
    dj_prepare.prepare(bus, "line", pool=env["pool"], now=t)
    dj_prepare.tick(bus, env["pool"], now=t, spawn=env["spawn"])
    inflight = env["spawn"].pending[0]
    env["pool"].stop()
    assert inflight.cancelled and not dj_prepare._TASKS
    env["spawn"].run_all()


def test_saved_clip_only_after_all_tries_fail(env, monkeypatch, ledger):
    _render_fail(monkeypatch)
    t = _play()
    dj_prepare.prepare(bus, "line", pool=env["pool"], now=t)
    _walk(env, t, t + 165)  # every try done, still > 15 s left
    assert len(_entry(env)["tries"]) == 4 and not _cmds("announce")
    _tick(env, t + 172)
    ann = _cmds("announce")
    assert len(ann) == 1 and ann[0].payload["clip_id"] == 555
    assert _entry(env)["fired_how"] == "saved_clip"
    db = ledger()
    assert db.query(DjClipUse).count() == 1


def test_saved_clip_capped_falls_back_to_pause(env, monkeypatch, ledger):
    db = ledger()
    db.add(DjClipUse(clip_key="bank:dj_next", voice="chris", used_at=datetime.utcnow() - timedelta(days=3)))
    db.commit()
    _render_fail(monkeypatch)
    t = _play()
    dj_prepare.prepare(bus, "line", pool=env["pool"], now=t)
    _walk(env, t, t + 175)
    assert not _cmds("announce") and _entry(env)["status"] == "pause_at_boundary"


@pytest.mark.parametrize("days,allowed", [
    ((3,), False), ((10, 20), False), ((10,), True), ((40,), True), ((), True),
])
def test_reuse_cap(ledger, days, allowed):
    db = ledger()
    for d in days:
        db.add(DjClipUse(clip_key="bank:k", voice="chris", used_at=datetime.utcnow() - timedelta(days=d)))
    db.commit()
    assert dj_prepare.reuse_allowed(db, "chris", "bank:k", time.time()) is allowed


def test_skipped_song_cancels_pending_tries(env, monkeypatch):
    calls = _render_fail(monkeypatch)
    t = _play()
    dj_prepare.prepare(bus, "line", pool=env["pool"], now=t)
    _tick(env, t)  # try 0 fails
    dj_prepare.tick(bus, env["pool"], now=t + 31, spawn=env["spawn"])  # try 1 in flight, not run
    inflight = env["spawn"].pending[0]
    _play(cur=50, nxt=51, pos_s=0.5)
    dj_triggers.on_state(bus.get_state(), bus, env["pool"], now=t + 32)
    assert inflight.cancelled
    assert _entry(env)["status"] == "dropped" and _entry(env)["drop_reason"] == "skipped"
    env["spawn"].run_all()
    t2 = _play(cur=50, nxt=51, pos_s=60)
    assert not _tick(env, t2 + 60)["started"]
    assert len(calls) == 1 and not _cmds("pause")


def test_no_try_starts_while_todd_talks_or_persona_on_air(env, monkeypatch):
    _render_ok(monkeypatch)
    t = _play()
    dj_prepare.prepare(bus, "line", pool=env["pool"], now=t)
    _speech(env, stt_active=True)
    assert not _tick(env, t)["started"] and _entry(env)["waiting"] == "todd_talking"
    _speech(env, agent_audio_until=datetime.fromtimestamp(time.time() + 30, tz=timezone.utc).isoformat())
    assert not _tick(env, t + 5)["started"] and _entry(env)["waiting"] == "persona_on_air"
    _speech(env)
    con = sqlite3.connect(env["pdb"])
    con.execute("CREATE TABLE speech_queue (id INTEGER PRIMARY KEY, timestamp TEXT, status TEXT)")
    con.execute("INSERT INTO speech_queue (timestamp, status) VALUES (?, 'pending')",
                (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),))
    con.commit()
    assert not _tick(env, t + 10)["started"] and _entry(env)["waiting"] == "persona_queued"
    con.execute("UPDATE speech_queue SET status = 'played'")
    con.commit()
    con.close()
    assert _tick(env, t + 15)["started"] and _entry(env)["status"] == "ready"


def test_todd_talking_at_fire_time_holds_then_fires(env, monkeypatch):
    _render_ok(monkeypatch)
    t = _play()
    dj_prepare.prepare(bus, "line", pool=env["pool"], now=t)
    _tick(env, t)
    _speech(env, stt_active=True)
    _tick(env, t + 172)
    assert not _cmds("announce") and _entry(env)["status"] == "held"
    _speech(env)
    _tick(env, t + 177)
    assert len(_cmds("announce")) == 1 and _entry(env)["status"] == "fired"


def test_prepare_refuses_a_song_that_is_not_playing(env):
    _play()
    r = dj_prepare.prepare(bus, "line", for_track=999, pool=env["pool"])
    assert not r["prepared"] and "not playing" in r["reason"]


def test_mcp_at_end_posts_to_server(monkeypatch):
    sent = []

    class Resp:
        status_code = 200
        headers = {"content-type": "application/json"}
        text = ""

        def json(self):
            return {"prepared": True, "id": "p1", "for_track": 11, "song_ends_in_s": 170.0, "replaced": []}

    class Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            sent.append((url, json))
            return Resp()

    monkeypatch.setattr(mcp_server, "_mute_hold", lambda kind: None)
    monkeypatch.setattr(mcp_server.httpx, "AsyncClient", Client)
    fn = getattr(mcp_server.dj_announce, "fn", mcp_server.dj_announce)
    out = asyncio.run(fn("hi", mode="at_end", for_track=11))
    assert sent and sent[0][0].endswith("/api/playback/pool/prepared")
    assert sent[0][1]["say"] == "hi" and sent[0][1]["for_track"] == 11
    assert "Prepared break p1" in out


def test_settings_survive_pool_stop(env):
    pool = env["pool"]
    assert dj_prepare.settings(pool)["fallback"] == "pause"
    dj_prepare.set_settings(pool, "silent")
    with pytest.raises(ValueError):
        dj_prepare.set_settings(pool, "loud")
    pool.stop()
    assert dj_prepare.settings(pool)["fallback"] == "silent"
