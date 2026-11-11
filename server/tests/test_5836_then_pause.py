"""#5836 step 3: a phone build that handles announce then="pause" stops itself at the outro clip's end.

The server sends then="pause" only to a phone reporting app_version_code >= THEN_PAUSE_MIN_VERSION_CODE,
and then sends no timed pause; it keeps only the late backstop (a real song started and is playing).
"""

import pytest

from audiplex import dj_pool, dj_triggers, playback_bus
from audiplex.playback_bus import bus


@pytest.fixture(autouse=True)
def engine(tmp_path, monkeypatch):  # #5836 same isolation as test_5836_outro_pause_and_hold
    speech = tmp_path / "speech_state.json"
    speech.write_text('{"stt_active": false, "talk_active": false, "composing": false}', encoding="utf-8")
    monkeypatch.setenv("DJ_SPEECH_STATE_FILE", str(speech))
    monkeypatch.delenv("AUDIPLEX_PUBLIC_URL", raising=False)
    monkeypatch.setattr(dj_triggers, "_client_base_url", "http://phone-sees-this:8100")
    monkeypatch.setattr(playback_bus, "_pool_session", lambda: None)
    scheduled = []
    monkeypatch.setattr(dj_triggers, "_schedule", lambda delay, fn: scheduled.append((delay, fn)))
    dj_triggers.reset_warnings()
    bus.reset()
    yield {"scheduled": scheduled}
    bus.reset()


def _state(current, playing=True, title="song", version=None, position_ms=0, duration_ms=0):
    state = {"playing": playing, "track": {"id": current, "title": title}, "queue_index": 0,
             "queue": [{"index": 0, "id": current, "title": title}],
             "position_ms": position_ms, "duration_ms": duration_ms}
    if version is not None:
        state["app_version_code"] = version
    return state


def _cmds(kind):
    return [r for r in bus._commands.values() if r.type == kind]


def _arm(client, version):
    bus.set_state(_state(20, version=version))
    client.post("/api/playback/pool/outro", json={"clip_id": 555, "duration_seconds": 4.0})
    announce, = _cmds("announce")
    return announce


@pytest.mark.parametrize("version", [None, dj_triggers.THEN_PAUSE_MIN_VERSION_CODE - 1])
def test_older_phone_gets_no_then_and_the_timed_pause(engine, client, version):  # #5836
    announce = _arm(client, version)
    assert "then" not in announce.payload and "fade_ms" not in announce.payload
    assert dj_pool.get_pool().state["outro"]["pause_state"] == "armed"
    bus.set_state(_state(-3, title=announce.payload["title"], version=version, position_ms=1000, duration_ms=4000))
    assert len(engine["scheduled"]) == 1  # the timed after-clip pause, as before


def test_new_phone_gets_then_pause_and_no_timed_pause(engine, client):  # #5836
    v = dj_triggers.THEN_PAUSE_MIN_VERSION_CODE
    announce = _arm(client, v)
    assert announce.payload["then"] == "pause"
    assert announce.payload["fade_ms"] == dj_triggers.THEN_PAUSE_FADE_MS
    outro = dj_pool.get_pool().state["outro"]
    assert outro["pause_state"] == "phone"
    title = announce.payload["title"]
    bus.set_state(_state(-3, title=title, version=v, position_ms=1000, duration_ms=4000))
    assert engine["scheduled"] == [] and _cmds("pause") == []  # the phone owns the stop
    bus.set_state(_state(-3, playing=False, title=title, version=v, position_ms=4000, duration_ms=4000))
    assert outro["pause_state"] == "paused_by_phone"
    bus.set_state(_state(21, version=v))  # Todd pressed play: the next song is his
    assert _cmds("pause") == []


def test_new_phone_that_rolled_into_a_song_gets_the_late_backstop(engine, client):  # #5836
    v = dj_triggers.THEN_PAUSE_MIN_VERSION_CODE
    announce = _arm(client, v)
    bus.set_state(_state(-3, title=announce.payload["title"], version=v, position_ms=1000, duration_ms=4000))
    bus.set_state(_state(21, version=v))  # playing a real song: the phone did not stop
    pause, = _cmds("pause")
    assert pause.source == "dj_triggers:outro_late"


def test_phone_paused_before_the_song_report_is_left_alone(engine, client):  # #5836
    v = dj_triggers.THEN_PAUSE_MIN_VERSION_CODE
    _arm(client, v)
    bus.set_state(_state(21, playing=False, version=v))  # never saw the clip report, but it is paused
    assert _cmds("pause") == []
    assert dj_pool.get_pool().state["outro"]["pause_state"] == "paused_by_phone"


def test_a_pause_early_in_the_clip_keeps_the_backstop(engine, client):  # #5836 a #5837 hold, or Todd's tap
    v = dj_triggers.THEN_PAUSE_MIN_VERSION_CODE
    announce = _arm(client, v)
    title = announce.payload["title"]
    bus.set_state(_state(-3, title=title, version=v, position_ms=0, duration_ms=4000))
    bus.set_state(_state(-3, playing=False, title=title, version=v, position_ms=200, duration_ms=4000))
    assert dj_pool.get_pool().state["outro"]["pause_state"] == "phone"
    bus.set_state(_state(21, version=v))  # the phone then rolled into a song without stopping
    pause, = _cmds("pause")
    assert pause.source == "dj_triggers:outro_late"
