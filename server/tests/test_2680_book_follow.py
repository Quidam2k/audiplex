"""#2680: an audiobook follows a transfer; a stale progress write can't clobber a newer one."""

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

from audiplex import playback_bus, routers
from audiplex.models import Book, PlaybackPosition, User
from audiplex.playback_bus import bus

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from audiplex_mcp import server as mcp_server  # noqa: E402

PC = {"device_id": "pc-solace", "device_name": "Solace", "device_type": "windows"}


@pytest.fixture(autouse=True)
def reset_bus(monkeypatch):
    bus.reset()
    monkeypatch.setattr(routers.playback, "LONGPOLL_TIMEOUT_SECONDS", 0.05)
    yield


@pytest.fixture
def book_db(db_engine, monkeypatch):
    """Point the handoff's book lookup at the test DB."""
    Session = sessionmaker(bind=db_engine)
    monkeypatch.setattr(playback_bus, "_book_session", Session)
    return Session


@pytest.fixture
def book(db_session):
    b = Book(title="Dune", author="Herbert", file_path="/x/dune.m4b",
             duration_seconds=3600, file_size=1)
    db_session.add(b)
    db_session.commit()
    return b


def _save_position(db, book_id, seconds, age_s):
    user = db.query(User).first()
    db.add(PlaybackPosition(
        book_id=book_id, user_id=user.id, position_seconds=seconds, chapter_index=0,
        updated_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=age_s),
    ))
    db.commit()


def poll(client, params=None):
    resp = client.get("/api/playback/command/next", params=params)
    return resp.json() if resp.status_code == 200 else None


def _transfer_from_phone(client, phone_state):
    poll(client)  # phone live
    poll(client, PC)
    client.post("/api/playback/state", json=phone_state)
    client.post("/api/playback/devices/pc-solace/activate")
    deactivate = poll(client)
    client.post(f"/api/playback/command/{deactivate['id']}/ack", json={"status": "ok"})
    return poll(client, PC)


PHONE_BOOK_STATE = {"playing": True, "track": None, "position_ms": 1_234_000, "duration_ms": 3_600_000}


def test_phone_book_follows_to_pc(client, db_session, book, book_db):
    _save_position(db_session, book.id, 1_220, age_s=20)
    activate = _transfer_from_phone(client, PHONE_BOOK_STATE)
    assert activate["type"] == "activate"
    assert activate["payload"]["track_ids"] == []
    assert activate["payload"]["book_id"] == book.id
    assert activate["payload"]["position_ms"] == 1_234_000  # the phone's fresher report
    assert activate["payload"]["playing"] is True


def test_phone_book_uses_saved_position_when_report_has_none(client, db_session, book, book_db):
    _save_position(db_session, book.id, 1_220, age_s=20)
    activate = _transfer_from_phone(client, {**PHONE_BOOK_STATE, "position_ms": 0})
    assert activate["payload"]["position_ms"] == 1_220_000


def test_stale_book_position_does_not_start_a_book(client, db_session, book, book_db):
    _save_position(db_session, book.id, 1_220, age_s=600)  # last saved 10 min ago
    activate = _transfer_from_phone(client, PHONE_BOOK_STATE)
    assert "book_id" not in activate["payload"]
    assert activate["payload"]["playing"] is False  # today's behavior: nothing to resume


def test_phone_playing_music_never_becomes_a_book(client, db_session, book, book_db):
    _save_position(db_session, book.id, 1_220, age_s=5)  # a fresh book save exists...
    music = {"playing": True, "track": {"id": 7}, "position_ms": 5000,
             "queue_index": 0, "queue": [{"index": 0, "id": 7}]}
    activate = _transfer_from_phone(client, music)  # ...but the phone is on music
    assert activate["payload"]["track_ids"] == [7]
    assert "book_id" not in activate["payload"]


def test_pc_book_follows_back_to_phone_payload(client):
    poll(client, PC)
    client.post("/api/playback/devices/pc-solace/activate")
    for rec in list(bus._commands.values()):
        bus.ack(rec.id, "ok")
    client.post("/api/playback/state?device_id=pc-solace", json={
        "playing": True, "track": None, "book": {"id": 42, "title": "Dune", "chapter_index": 3},
        "position_ms": 900_000, "duration_ms": 3_600_000,
    })
    assert client.get("/api/playback/state?device_id=pc-solace").json()["book"]["id"] == 42
    poll(client)  # phone live
    client.post("/api/playback/devices/phone/activate")
    deactivate = poll(client, PC)
    assert deactivate["type"] == "deactivate"
    client.post(f"/api/playback/command/{deactivate['id']}/ack", json={"status": "ok"})
    activate = poll(client)
    assert activate["payload"]["book_id"] == 42
    assert activate["payload"]["position_ms"] == 900_000


# ---- rider: a late PC push never clobbers a newer phone position ----

def test_late_pc_push_loses_to_newer_phone_position(client, book):
    sampled_on_pc = datetime.now(timezone.utc) - timedelta(seconds=30)
    client.put(f"/api/progress/{book.id}", json={"position_seconds": 500})  # the phone, now
    late = client.put(f"/api/progress/{book.id}", json={
        "position_seconds": 400, "client_updated_at": sampled_on_pc.isoformat(),
    })
    assert late.status_code == 409
    assert late.json()["detail"]["reason"] == "stale"
    assert client.get(f"/api/progress/{book.id}").json()["position_seconds"] == 500


def test_fresh_pc_push_wins_and_phone_style_push_is_unchanged(client, book):
    client.put(f"/api/progress/{book.id}", json={"position_seconds": 500})
    fresh = datetime.now(timezone.utc) + timedelta(seconds=1)
    ok = client.put(f"/api/progress/{book.id}", json={
        "position_seconds": 600, "client_updated_at": fresh.isoformat(),
    })
    assert ok.status_code == 200
    assert client.get(f"/api/progress/{book.id}").json()["position_seconds"] == 600
    # The phone sends no client_updated_at and keeps last-write-wins.
    assert client.put(f"/api/progress/{book.id}", json={"position_seconds": 10}).status_code == 200


# ---- MCP ----

@pytest.fixture
def wire(monkeypatch):
    calls = []

    async def fake_get(path):
        calls.append(("GET", path))
        if path == "/api/library/books":
            return [{"id": 1, "title": "Dune"}, {"id": 2, "title": "Dune Messiah"}, {"id": 3, "title": "Emma"}]
        return {}

    async def fake_raw(cmd, payload):
        calls.append(("CMD", cmd, payload))
        return {"id": 5, "pending": 1}

    async def fake_put(path, body):  # #3713: an explicit start is saved first
        calls.append(("PUT", path, body))
        return {}

    monkeypatch.setattr(mcp_server, "_get", fake_get)
    monkeypatch.setattr(mcp_server, "_put", fake_put)
    monkeypatch.setattr(mcp_server, "_enqueue_raw", fake_raw)
    return calls


def test_dj_play_book_resolves_title_and_resumes(wire):
    out = asyncio.run(mcp_server.dj_play_book("dune"))  # exact title beats the substring match
    assert ("CMD", "play_book", {"book_id": 1, "playing": True}) in wire
    assert "Dune" in out


def test_dj_play_book_ambiguous_and_explicit_position(wire):
    assert "pick one by id" in asyncio.run(mcp_server.dj_play_book("un"))
    asyncio.run(mcp_server.dj_play_book("3", position_seconds=90))
    assert ("CMD", "play_book", {"book_id": 3, "playing": True, "position_ms": 90_000}) in wire


# ---- #3713: start a book at a computed spot, or just save the spot ----

@pytest.fixture
def wire3713(wire, monkeypatch):
    async def fake_get(path):
        wire.append(("GET", path))
        if path == "/api/library/books":
            return [
                {"id": 1, "title": "Dune", "duration_seconds": 100000},
                {"id": 2, "title": "Dune Messiah", "duration_seconds": 100000},
                {"id": 3, "title": "Emma", "duration_seconds": 100000},
            ]
        if path == "/api/library/books/1":
            return {"chapters": [{"start_seconds": 0}, {"start_seconds": 50000}]}
        return {}

    monkeypatch.setattr(mcp_server, "_get", fake_get)
    return wire


def test_dj_play_book_from_end_saves_before_command(wire3713):
    asyncio.run(mcp_server.dj_play_book("1", from_end_seconds=61800))
    put = ("PUT", "/api/progress/1", {"position_seconds": 38200, "chapter_index": 0})
    cmd = ("CMD", "play_book", {"book_id": 1, "playing": True, "position_ms": 38_200_000})
    assert put in wire3713
    assert cmd in wire3713
    assert wire3713.index(put) < wire3713.index(cmd)


@pytest.mark.parametrize("tool", [mcp_server.dj_play_book, mcp_server.dj_set_book_position])
def test_book_position_refuses_both_arguments(wire3713, tool):
    out = asyncio.run(tool("1", position_seconds=10, from_end_seconds=20))
    assert "not both" in out
    assert not any(call[0] in ("CMD", "PUT") for call in wire3713)


def test_dj_play_book_refuses_from_end_beyond_duration(wire3713):
    out = asyncio.run(mcp_server.dj_play_book("1", from_end_seconds=100001))
    assert "longer than" in out
    assert not any(call[0] in ("CMD", "PUT") for call in wire3713)


def test_dj_set_book_position_saves_chapter_and_requires_position(wire3713):
    asyncio.run(mcp_server.dj_set_book_position("1", position_seconds=60000))
    assert ("PUT", "/api/progress/1", {"position_seconds": 60000, "chapter_index": 1}) in wire3713
    assert not any(call[0] == "CMD" for call in wire3713)
    wire3713.clear()
    out = asyncio.run(mcp_server.dj_set_book_position("1"))
    assert "Give position_seconds or from_end_seconds" in out
    assert not any(call[0] in ("CMD", "PUT") for call in wire3713)


def test_dj_play_book_saved_position_does_not_write(wire3713):
    asyncio.run(mcp_server.dj_play_book("1"))
    assert ("CMD", "play_book", {"book_id": 1, "playing": True}) in wire3713
    assert not any(call[0] == "PUT" for call in wire3713)


def test_dj_play_book_old_phone_suggests_continue(wire3713, monkeypatch):
    async def fake_result(data, wait):
        return "RESULT ... phone_ack=unknown_type: play_book playing=NO\n"

    monkeypatch.setattr(mcp_server, "_result", fake_result)
    out = asyncio.run(mcp_server.dj_play_book("1", from_end_seconds=61800))
    assert "too old" in out
    assert "Continue" in out
