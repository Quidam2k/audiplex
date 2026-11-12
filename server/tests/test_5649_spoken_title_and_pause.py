"""#5649: spoken_title cleans video-style names for DJ lines, and the after-outro pause survives an unknown clip length."""
import json

import pytest

from audiplex import dj_pool, dj_triggers, playback_bus
from audiplex.playback_bus import bus
from audiplex.tag_repair import spoken_title


@pytest.fixture(autouse=True)
def engine(tmp_path, monkeypatch):
    speech = tmp_path / "speech_state.json"
    monkeypatch.setenv("DJ_SPEECH_STATE_FILE", str(speech))
    monkeypatch.delenv("AUDIPLEX_PUBLIC_URL", raising=False)
    monkeypatch.setattr(dj_triggers, "_client_base_url", "http://phone-sees-this:8100")
    monkeypatch.setattr(playback_bus, "_pool_session", lambda: None)
    scheduled = []
    monkeypatch.setattr(dj_triggers, "_schedule", lambda delay, fn: scheduled.append((delay, fn)))
    dj_triggers.reset_warnings()
    bus.reset()
    yield {"speech": speech, "scheduled": scheduled}
    bus.reset()


def _speech(engine, talking=False):
    data = {"stt_active": talking, "talk_active": False, "composing": False, "todd_last_spoke_at": None}
    engine["speech"].write_text(json.dumps(data), encoding="utf-8")


def _state(current, after=(), playing=True, title="song", position_ms=0, duration_ms=0):
    queue = [{"index": 0, "id": current, "title": title}] + [
        {"index": i + 1, "id": t, "title": f"t{t}"} for i, t in enumerate(after)]
    return {"playing": playing, "track": {"id": current, "title": title}, "queue_index": 0,
            "queue": queue, "position_ms": position_ms, "duration_ms": duration_ms}


def _cmds(kind=None):
    return [r for r in bus._commands.values() if kind is None or r.type == kind]


# ----- PART 1: spoken_title -----

@pytest.mark.parametrize("title, artist, expected", [
    ("Playground | Arcane League of Legends and K/DA - ... Official Concept Video", None, "Playground"),
    ("Heaven or Las Vegas", "Cocteau Twins", "Heaven or Las Vegas"),
    ("Make It Official", None, "Make It Official"),
    ("Billie Eilish - bury a friend (Official Video)", "Billie Eilish", "bury a friend"),
    ("Billie Eilish - bury a friend (Official Video)", None, "Billie Eilish - bury a friend"),
    ('Arcane: Season 2 | "Paint The Town Blue"', None, "Paint The Town Blue"),
    ("All You Need Is Love (Remastered 2009)", None, "All You Need Is Love (Remastered 2009)"),
    ("Spanish Lady (Live)", None, "Spanish Lady (Live)"),
    ("Song - Official Music Video", None, "Song"),
    ("Song (Official Concept Video)", None, "Song"),
    ("Song (Lyric Video)", None, "Song"),
    ("Dance (Video Killed the Radio Star)", None, "Dance (Video Killed the Radio Star)"),
    ("Song (Lyrics of Love)", None, "Song (Lyrics of Love)"),
    ("Title | Second Part", None, "Title"),  # a real " | " keeps its leading segment (pinned)
])
def test_spoken_title_cleaning(title, artist, expected):  # #5649
    assert spoken_title(title, artist) == expected


@pytest.mark.parametrize("title", ["Official Video", "(Official Video)"])
def test_spoken_title_never_empty(title):  # #5649
    assert spoken_title(title)


@pytest.mark.parametrize("title, expected", [(None, ""), ("", "")])
def test_spoken_title_empty_input(title, expected):  # #5649
    assert spoken_title(title) == expected


# ----- PART 2: /api/playback/state -----

def test_playback_state_adds_spoken_title(client):  # #5649
    raw_track = "Playground | Arcane League of Legends and K/DA - ... Official Concept Video"
    raw_next = "Song (Official Concept Video)"
    raw_plain = "Heaven or Las Vegas"
    bus.set_state({
        "playing": False,
        "track": {"id": 20, "title": raw_track},
        "queue_index": 0,
        "queue": [
            {"index": 0, "id": 20, "title": raw_track},
            {"index": 1, "id": 21, "title": raw_next},
            {"index": 2, "id": 22, "title": raw_plain},
            {"index": 3, "id": 23},
        ],
        "position_ms": 0,
        "duration_ms": 0,
    })
    body = client.get("/api/playback/state").json()

    assert body["track"]["title"] == raw_track
    assert body["track"]["spoken_title"] == "Playground"

    items = {item["id"]: item for item in body["queue"]}
    assert items[20]["title"] == raw_track
    assert items[20]["spoken_title"] == "Playground"
    assert items[21]["title"] == raw_next
    assert items[21]["spoken_title"] == "Song"
    assert items[22]["title"] == raw_plain
    assert items[22]["spoken_title"] == raw_plain
    assert "title" not in items[23]
    assert "spoken_title" not in items[23]


# ----- PART 3: after-outro pause when the clip length is unknown -----

def _arm_outro(client, engine, **extra):
    _speech(engine)
    bus.set_state(_state(20, after=[21]))
    client.post("/api/playback/pool/outro", json={"clip_id": 555, **extra})
    return _cmds("announce")[0].payload["title"]


def _outro_pause_state():
    return dj_pool.get_pool().state["outro"]["pause_state"]


def test_unknown_clip_length_waits_for_a_report_with_one(client, engine):  # #5649
    title = _arm_outro(client, engine)

    bus.set_state(_state(-3, title=title, position_ms=1000, duration_ms=0))
    assert engine["scheduled"] == []
    assert _outro_pause_state() == "armed"
    assert _cmds("pause") == []

    bus.set_state(_state(-3, title=title, position_ms=1500, duration_ms=4000))
    (delay, fn), = engine["scheduled"]
    assert delay == pytest.approx(2.5 - dj_triggers.PAUSE_LEAD_S, abs=0.05)
    assert _outro_pause_state() == "pausing"

    fn()
    assert len(_cmds("pause")) == 1


def test_unknown_clip_length_never_reported_late_backstop(client, engine):  # #5649
    title = _arm_outro(client, engine)

    bus.set_state(_state(-3, title=title, position_ms=1000, duration_ms=0))
    assert engine["scheduled"] == []
    assert _cmds("pause") == []

    bus.set_state(_state(21))
    assert len(_cmds("pause")) == 1
    assert _outro_pause_state() == "paused_late"


def test_known_clip_duration_schedules_at_first_report(client, engine):  # #5649
    title = _arm_outro(client, engine, duration_seconds=4.0)

    bus.set_state(_state(-3, title=title, position_ms=1000, duration_ms=0))
    (delay, _fn), = engine["scheduled"]
    assert delay == pytest.approx(3.0 - dj_triggers.PAUSE_LEAD_S, abs=0.05)
    assert _outro_pause_state() == "pausing"
