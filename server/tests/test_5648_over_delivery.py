"""#5648 P3: DJ breaks with delivery="over" (Todd 59058, Pantheon asg 11721).

An "over" break is handed to the external DJ_OVER_MUSIC_CMD hook at T-30 s: with its
clip (--wav) when one is ready, otherwise with --fallback-say. It never pauses. The hook
is a recorder here; nothing is spawned and nothing reaches a phone.
"""

import asyncio
import json
import os  # #5648
import sys
from pathlib import Path

import pytest

from audiplex import dj_pool, dj_prepare, dj_triggers, playback_bus
from audiplex.playback_bus import bus

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from audiplex_mcp import server as mcp_server  # noqa: E402

CUR, NXT = 11, 12
HOOK = "python hook.py"


class Dummy:
    def __init__(self, coro):
        self.coro, self.cancelled = coro, False

    def done(self):
        return False

    def cancel(self):
        self.cancelled = True


class Spawner:
    """Holds each render try until the test runs it (run_all, or run_one for a slow render)."""

    def __init__(self):
        self.pending: list[Dummy] = []

    def __call__(self, coro):
        d = Dummy(coro)
        self.pending.append(d)
        return d

    def run_all(self):
        while self.pending:
            self.run_one()

    def run_one(self):
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
    monkeypatch.setenv("DJ_OVER_MUSIC_CMD", HOOK)
    monkeypatch.setattr(playback_bus, "_pool_session", lambda: None)
    monkeypatch.setattr(dj_triggers, "_schedule", lambda delay, fn: None)
    monkeypatch.setattr(dj_prepare, "_bank_clip", lambda entry: None)
    monkeypatch.setattr(bus, "_maybe_run_triggers", lambda state, device_key: None)
    dj_triggers.reset_warnings()
    dj_prepare._TASKS.clear()
    bus.reset()
    pool = dj_pool.get_pool()
    pool.state["prepared"] = []
    pool.state["outro"] = None
    pool.state.pop("prepare_settings", None)
    speech.write_text(json.dumps({"stt_active": False}), encoding="utf-8")
    hooks: list[list[str]] = []
    monkeypatch.setattr(dj_prepare, "_spawn_over_hook", lambda argv, entry_id: hooks.append(argv),
                        raising=False)
    yield {"speech": speech, "log": tmp_path / "log.jsonl", "pool": pool, "spawn": Spawner(), "hooks": hooks}
    bus.reset()


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


def _render_ok(monkeypatch, clip_id=777, wav="C:/dj/clip-777.wav"):
    async def ok(text, title):
        return {"clip_id": clip_id, "duration_seconds": 4.0, "rendered_at": 0.0, "wav_path": wav}
    monkeypatch.setattr(dj_prepare, "_render", ok)


def _render_fail(monkeypatch):
    calls = []

    async def fail(text, title):
        calls.append(text)
        raise RuntimeError("athena down")
    monkeypatch.setattr(dj_prepare, "_render", fail)
    return calls


def _entry(env):
    return env["pool"].state["prepared"][-1]


def _tick(env, now, run=True):
    out = dj_prepare.tick(bus, env["pool"], now=now, spawn=env["spawn"])
    if run:
        env["spawn"].run_all()
    return out


def _walk(env, t, until, step=5.0):
    starts, now = [], t
    while now <= until:
        if _tick(env, now)["started"]:
            starts.append(now)
        now += step
    return starts


def _argv_value(argv, flag):
    return argv[argv.index(flag) + 1]


def _log_lines(env):
    return [json.loads(x) for x in env["log"].read_text(encoding="utf-8").splitlines()]


def _pauses():
    return [r for r in bus._commands.values() if r.type == "pause"]


def test_over_hands_off_clip_at_T_minus_30(env, monkeypatch):
    _render_ok(monkeypatch)
    t = _play()  # ends at t + 185
    assert dj_prepare.prepare(bus, "That was X, here's Y", pool=env["pool"], now=t,
                              expect_next=NXT, agent="sam", delivery="over")["prepared"]
    _tick(env, t)
    assert _entry(env)["status"] == "ready"
    _tick(env, t + 150)  # 35 s left: too early
    assert env["hooks"] == []
    _tick(env, t + 155)  # 30 s left
    assert len(env["hooks"]) == 1
    argv = env["hooks"][0]
    assert argv[:2] == ["python", "hook.py"]
    assert _argv_value(argv, "--wav") == "C:/dj/clip-777.wav"
    assert "--fallback-say" not in argv
    assert _argv_value(argv, "--text") == "That was X, here's Y"
    assert float(_argv_value(argv, "--end-epoch")) == pytest.approx(t + 185)
    assert _argv_value(argv, "--track-id") == str(CUR)
    assert _argv_value(argv, "--next-track-id") == str(NXT)
    assert _argv_value(argv, "--persona") == "sam"
    assert _entry(env)["status"] == "handed_off"
    assert not _pauses()


def test_over_no_clip_by_T_minus_30_runs_fallback_say_with_same_args(env, monkeypatch):
    calls = _render_fail(monkeypatch)
    t = _play()
    dj_prepare.prepare(bus, "the same words", pool=env["pool"], now=t, expect_next=NXT,
                       agent="sam", delivery="over")
    _walk(env, t, t + 155)
    assert len(calls) == 4  # every try failed before the hand-off
    assert len(env["hooks"]) == 1
    argv = env["hooks"][0]
    assert "--fallback-say" in argv and "--wav" not in argv
    assert _argv_value(argv, "--text") == "the same words"
    assert float(_argv_value(argv, "--end-epoch")) == pytest.approx(t + 185)
    assert _argv_value(argv, "--track-id") == str(CUR)
    assert _argv_value(argv, "--next-track-id") == str(NXT)
    assert _argv_value(argv, "--persona") == "sam"
    assert _entry(env)["status"] == "fallback_sent"


def test_over_never_pauses_even_when_every_try_fails(env, monkeypatch):
    _render_fail(monkeypatch)
    t = _play()
    dj_prepare.prepare(bus, "line", pool=env["pool"], now=t, expect_next=NXT, delivery="over")
    _walk(env, t, t + 155)
    assert len(env["hooks"]) == 1
    # the song then ends and the next one starts: still no pause
    _play(cur=NXT, nxt=13, pos_s=0.5)
    dj_triggers.on_state(bus.get_state(), bus, env["pool"], now=t + 186)
    assert not _pauses()
    assert _entry(env)["status"] == "fallback_sent"
    assert not any(x["event"] == "fallback_pause" for x in _log_lines(env))


def test_over_next_track_change_goes_to_fallback(env, monkeypatch):
    _render_ok(monkeypatch)
    t = _play()
    dj_prepare.prepare(bus, "next up", pool=env["pool"], now=t, expect_next=NXT, delivery="over")
    _tick(env, t)
    assert _entry(env)["status"] == "ready"
    # Todd reordered the queue: 99 is next now, and the song has 30 s left
    t2 = _play(nxt=99, pos_s=170.0)
    _tick(env, t2)
    assert len(env["hooks"]) == 1
    argv = env["hooks"][0]
    assert "--fallback-say" in argv and "--wav" not in argv
    assert _argv_value(argv, "--text") == "next up"
    assert _argv_value(argv, "--next-track-id") == str(NXT)
    assert "next track changed" in _entry(env)["failed"]


def test_over_late_clip_after_fallback_gives_exactly_one_delivery(env, monkeypatch):
    slow = {}

    async def render(text, title):
        slow["done"] = True
        return {"clip_id": 9, "duration_seconds": 3.0, "rendered_at": 0.0, "wav_path": "C:/dj/late.wav"}

    monkeypatch.setattr(dj_prepare, "_render", render)
    t = _play()
    dj_prepare.prepare(bus, "line", pool=env["pool"], now=t, expect_next=NXT, delivery="over")
    _tick(env, t, run=False)  # the first try is in flight and has not finished
    assert len(env["spawn"].pending) == 1
    _tick(env, t + 155, run=False)  # no clip by T-30: the fallback goes
    assert len(env["hooks"]) == 1 and "--fallback-say" in env["hooks"][0]
    env["spawn"].run_one()  # the render lands late
    assert "late.wav" not in str(_entry(env).get("wav_path"))  # never adopted as the clip
    assert _entry(env)["status"] == "fallback_sent"
    assert any(x["event"] == "late_render_discarded" for x in _log_lines(env))
    _tick(env, t + 160)
    _tick(env, t + 165)
    assert len(env["hooks"]) == 1


def test_over_clip_first_then_no_fallback(env, monkeypatch):
    _render_ok(monkeypatch)
    t = _play()
    dj_prepare.prepare(bus, "line", pool=env["pool"], now=t, expect_next=NXT, delivery="over")
    _tick(env, t)
    _tick(env, t + 155)
    assert len(env["hooks"]) == 1 and "--wav" in env["hooks"][0]
    for dt in (160, 165, 170):
        _tick(env, t + dt)
    assert len(env["hooks"]) == 1


def test_over_last_try_starts_no_later_than_T_minus_40(env, monkeypatch):
    calls = _render_fail(monkeypatch)
    t = _play()
    end = t + 185
    dj_prepare.prepare(bus, "line", pool=env["pool"], now=t, delivery="over")
    starts = _walk(env, t, t + 150)
    assert len(calls) == 4
    assert max(starts) <= end - dj_prepare.OVER_LAST_TRY_LEAD_S
    # a try due inside the last 40 s never starts
    assert dj_prepare.try_due_times(t, end, "over")[-1] <= end - dj_prepare.OVER_LAST_TRY_LEAD_S


def test_over_short_song_scales_tries_inside_T_minus_40(env, monkeypatch):
    calls = _render_fail(monkeypatch)
    t = _play(pos_s=110.0, dur_s=200.0)  # 90 s left
    dj_prepare.prepare(bus, "line", pool=env["pool"], now=t, delivery="over")
    starts = _walk(env, t, t + 60, step=2.0)
    assert len(calls) == 4
    assert max(starts) <= t + 90 - dj_prepare.OVER_LAST_TRY_LEAD_S


def test_insert_still_pauses_when_every_try_fails(env, monkeypatch):
    _render_fail(monkeypatch)
    t = _play()
    dj_prepare.prepare(bus, "line", pool=env["pool"], now=t)  # default delivery
    assert _entry(env)["delivery"] == "insert"
    _walk(env, t, t + 175)
    assert _entry(env)["status"] == "pause_at_boundary" and not env["hooks"]
    _play(cur=NXT, nxt=13, pos_s=0.5)
    dj_triggers.on_state(bus.get_state(), bus, env["pool"], now=t + 186)
    assert [r for r in _pauses() if r.source == "dj_prepare:fallback_pause"]


def test_over_skipped_song_is_cancelled_without_pause_or_hook(env, monkeypatch):
    _render_ok(monkeypatch)
    t = _play()
    dj_prepare.prepare(bus, "line", pool=env["pool"], now=t, expect_next=NXT, delivery="over")
    _tick(env, t)
    dj_prepare.on_boundary(99, bus, env["pool"], now=t + 50)  # Todd skipped at 50 s
    assert _entry(env)["status"] == "dropped" and _entry(env)["drop_reason"] == "skipped"
    _tick(env, t + 155)
    assert env["hooks"] == [] and not _pauses()


def test_over_hook_defaults_to_pantheon_script_when_unset(monkeypatch):  # #5648
    monkeypatch.delenv("DJ_OVER_MUSIC_CMD", raising=False)  # #5648
    cmd = dj_prepare.over_cmd()  # #5648
    assert cmd.endswith("dj_over_music.py") and "python.exe" in cmd  # #5648


def test_server_tts_env_defaults_only_when_a_voice_is_set(monkeypatch):  # #5648

    for k in ("DJ_TTS_URL", "DJ_TTS_CMD", "DJ_TTS_VOICE"):  # #5648
        monkeypatch.delenv(k, raising=False)  # #5648
    dj_triggers.ensure_server_tts_env()  # #5648
    assert "DJ_TTS_CMD" not in os.environ  # #5648 no voice = stays unconfigured
    monkeypatch.setenv("DJ_TTS_VOICE", "chris")  # #5648
    dj_triggers.ensure_server_tts_env()  # #5648
    cmd = os.environ["DJ_TTS_CMD"]  # #5648
    assert "tts_render_cli.py" in cmd and "--agent {voice}" in cmd and "{out}" in cmd  # #5648
    monkeypatch.setenv("DJ_TTS_CMD", "mine {text} {out}")  # #5648
    dj_triggers.ensure_server_tts_env()  # #5648
    assert os.environ["DJ_TTS_CMD"] == "mine {text} {out}"  # #5648 an operator's command wins


def test_server_tts_env_takes_athena_url_from_local_config(monkeypatch):  # #5648
    for k in ("DJ_TTS_URL", "DJ_TTS_CMD", "PANTHEON_TTS_REMOTE_URL"):  # #5648
        monkeypatch.delenv(k, raising=False)  # #5648
    monkeypatch.setenv("DJ_TTS_VOICE", "chris")  # #5648
    monkeypatch.setattr(dj_triggers.audiplex_paths, "root",  # #5648
                        lambda key: "http://athena.test:7779" if key == "tts_remote_url" else "")  # #5648
    dj_triggers.ensure_server_tts_env()  # #5648
    assert os.environ["PANTHEON_TTS_REMOTE_URL"] == "http://athena.test:7779"  # #5648


def test_over_without_hook_command_logs_and_does_nothing(env, monkeypatch):
    monkeypatch.setenv("DJ_OVER_MUSIC_CMD", "")  # #5648 '' = hook turned off
    _render_ok(monkeypatch)
    t = _play()
    dj_prepare.prepare(bus, "line", pool=env["pool"], now=t, delivery="over")
    _tick(env, t)
    _tick(env, t + 155)
    assert env["hooks"] == [] and not _pauses()
    assert _entry(env)["status"] == "no_hook"
    assert any(x["event"] == "handoff_skipped" for x in _log_lines(env))


def test_over_hook_argv_keeps_quoted_command_and_text_with_spaces(env, monkeypatch):
    monkeypatch.setenv("DJ_OVER_MUSIC_CMD", '"C:/Program Files/py/python.exe" hook.py')
    _render_fail(monkeypatch)
    t = _play()
    dj_prepare.prepare(bus, "a line with spaces", pool=env["pool"], now=t, delivery="over")
    _walk(env, t, t + 155)
    argv = env["hooks"][0]
    assert argv[0] == "C:/Program Files/py/python.exe" and argv[1] == "hook.py"
    assert _argv_value(argv, "--text") == "a line with spaces"


def test_prepare_rejects_unknown_delivery(env):
    t = _play()
    with pytest.raises(ValueError):
        dj_prepare.prepare(bus, "line", pool=env["pool"], now=t, delivery="whisper")


def test_wav_is_kept_on_disk_for_the_hook(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIPLEX_DJ_WAV_DIR", str(tmp_path / "wavs"))
    src = tmp_path / "tts.wav"
    src.write_bytes(b"RIFF")
    kept = Path(dj_triggers._keep_render(src, 42))
    assert kept.is_file() and kept.read_bytes() == b"RIFF" and kept.parent == tmp_path / "wavs"


def test_mcp_dj_announce_passes_delivery_through(monkeypatch):
    sent = {}

    class Resp:
        status_code = 200
        headers = {"content-type": "application/json"}
        text = "{}"

        def json(self):
            return {"id": "p1", "for_track": CUR, "song_ends_in_s": 150.0}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            sent.update(json)
            return Resp()

    monkeypatch.setattr(mcp_server.httpx, "AsyncClient", FakeClient)
    fn = getattr(mcp_server.dj_announce, "fn", mcp_server.dj_announce)
    asyncio.run(fn("a line", mode="at_end", delivery="over"))
    assert sent["delivery"] == "over"
    asyncio.run(fn("a line", mode="at_end"))
    assert sent["delivery"] == "insert"
    assert "whisper" in asyncio.run(fn("a line", mode="at_end", delivery="whisper"))


def test_route_accepts_delivery_and_reports_the_entry(client, env, monkeypatch):
    _play()
    r = client.post("/api/playback/pool/prepared", json={"say": "hi", "delivery": "over"})
    assert r.status_code == 200 and r.json()["delivery"] == "over"
    got = client.get("/api/playback/pool/prepared", params={"track_id": CUR}).json()
    assert got["exists"] is True and got["entry"]["delivery"] == "over"
    assert client.get("/api/playback/pool/prepared", params={"track_id": 999}).json()["exists"] is False
    bad = client.post("/api/playback/pool/prepared", json={"say": "hi", "delivery": "whisper"})
    assert bad.status_code == 400
