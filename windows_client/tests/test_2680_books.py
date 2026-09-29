"""#2680: audiobooks on the PC renderer: play/resume, chapters, progress sync, sleep."""
from __future__ import annotations

import time

import pytest

from audiplex_pc import bus as bus_module
from audiplex_pc.bus import BusClient
from audiplex_pc.player import BookInfo, Player, QueueItem

PROXY = "http://127.0.0.1:5555"

M4B = {
    "id": 7, "title": "Dune", "author": "Herbert", "duration_seconds": 3600,
    "chapters": [
        {"index": 0, "title": "One", "start_seconds": 0},
        {"index": 1, "title": "Two", "start_seconds": 600},
        {"index": 2, "title": "Three", "start_seconds": 1800},
    ],
    "track_urls": [],
}

MULTI = {
    "id": 8, "title": "Emma", "author": "Austen", "duration_seconds": 300,
    "chapters": [
        {"index": 0, "title": "01", "start_seconds": 0},
        {"index": 1, "title": "02", "start_seconds": 100},
        {"index": 2, "title": "03", "start_seconds": 200},
    ],
    "track_urls": ["/api/stream/8/track/0", "/api/stream/8/track/1", "/api/stream/8/track/2"],
}


class Resp:
    def __init__(self, status: int, body=None) -> None:
        self.status_code = status
        self._body = body

    def json(self):
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeHttp:
    """Stands in for BusClient.client (httpx.Client)."""

    def __init__(self) -> None:
        self.books = {7: M4B, 8: MULTI}
        self.progress: dict[int, dict] = {}
        self.puts: list[tuple[str, dict]] = []
        self.put_status = 200

    def get(self, path, params=None):
        if path.startswith("/api/library/books/"):
            book = self.books.get(int(path.rsplit("/", 1)[-1]))
            return Resp(200, book) if book else Resp(404)
        if path.startswith("/api/progress/"):
            saved = self.progress.get(int(path.rsplit("/", 1)[-1]))
            return Resp(200, saved) if saved else Resp(404)
        return Resp(204)

    def put(self, path, json=None):
        self.puts.append((path, json))
        return Resp(self.put_status, {})

    def post(self, path, params=None, json=None):
        return Resp(200, {})

    def close(self) -> None:
        pass


def _bus(player: Player | None = None) -> tuple[BusClient, Player, FakeHttp]:
    player = player or Player()
    client = BusClient("http://127.0.0.1:1", "t", "pc", "PC", player, PROXY)
    http = FakeHttp()
    client.client = http
    return client, player, http


def _main(player: Player):
    return player._instance.players[0]


def _wait(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# --- play_book ----------------------------------------------------------------

def test_m4b_resumes_from_the_servers_saved_position():
    client, player, http = _bus()
    http.progress[7] = {"position_seconds": 1234.5, "is_finished": False}
    assert client.dispatch({"type": "play_book", "payload": {"book_id": 7}}) == ("ok", "")
    media = _main(player).media
    assert media.url == f"{PROXY}/api/stream/7"
    assert ":start-time=1234.500" in media.options
    state = player.state()
    assert state["track"] is None  # books never pose as music tracks
    assert state["book"] == {"id": 7, "title": "Dune", "chapter_index": 0}
    assert state["duration_ms"] == 3_600_000


def test_finished_or_unsaved_book_starts_at_zero_and_explicit_position_wins():
    client, player, http = _bus()
    http.progress[7] = {"position_seconds": 3599, "is_finished": True}
    client.dispatch({"type": "play_book", "payload": {"book_id": 7}})
    assert not any(o.startswith(":start-time") for o in _main(player).media.options)
    client.dispatch({"type": "play_book", "payload": {"book_id": 7, "position_ms": 700_000}})
    assert ":start-time=700.000" in _main(player).media.options


def test_unknown_book_and_bad_payload():
    client, _, _ = _bus()
    assert client.dispatch({"type": "play_book", "payload": {"book_id": 99}})[0] == "no_book"
    assert client.dispatch({"type": "play_book", "payload": {}})[0] == "bad_payload"


def test_multi_file_book_starts_in_the_right_part_with_a_global_position():
    client, player, _ = _bus()
    client.dispatch({"type": "play_book", "payload": {"book_id": 8, "position_ms": 150_000}})
    assert player.index == 1
    main = _main(player)
    assert main.media.url == f"{PROXY}/api/stream/8/track/1"
    assert ":start-time=50.000" in main.media.options
    main.time_ms = 50_000
    assert player.state()["position_ms"] == 150_000
    assert player.state()["book"]["chapter_index"] == 1
    assert [q["id"] for q in player.state()["queue"]] == [-1, -1]  # no real tracks to hand off


def test_activate_with_a_book_resumes_it_paused_when_asked():
    client, player, _ = _bus()
    status = client.dispatch({"type": "activate", "payload": {
        "track_ids": [], "book_id": 7, "position_ms": 900_000, "playing": False}})
    assert status == ("ok", "")
    assert ":start-time=900.000" in _main(player).media.options
    assert player._pause_on_start is True
    # A handoff with neither tracks nor a book still just makes us the target.
    assert client.dispatch({"type": "activate", "payload": {"track_ids": []}}) == ("ok", "")


# --- chapters -----------------------------------------------------------------

def _dune(player: Player, at_ms: int) -> None:
    client, _, _ = _bus(player)
    client.dispatch({"type": "play_book", "payload": {"book_id": 7, "position_ms": at_ms}})
    _main(player).time_ms = at_ms


def test_skip_goes_to_the_next_chapter_and_stops_at_the_last():
    player = Player()
    _dune(player, 700_000)
    player.skip()
    assert _main(player).time_ms == 1_800_000
    player.skip()
    assert _main(player).time_ms == 1_800_000  # last chapter: nowhere to go
    assert player.book is not None


def test_previous_restarts_the_chapter_then_goes_back_one():
    player = Player()
    _dune(player, 700_000)
    player.previous()
    assert _main(player).time_ms == 600_000
    player.previous()  # now at the chapter start: back one chapter
    assert _main(player).time_ms == 0


def test_seek_is_book_global_across_parts():
    client, player, _ = _bus()
    client.dispatch({"type": "play_book", "payload": {"book_id": 8, "position_ms": 10_000}})
    assert player.index == 0
    player.seek(250_000)
    assert player.index == 2
    assert ":start-time=50.000" in _main(player).media.options


def test_music_after_a_book_clears_it():
    client, player, _ = _bus()
    client.dispatch({"type": "play_book", "payload": {"book_id": 7, "position_ms": 5000}})
    player.play_now([QueueItem("track", 3, "Song", "A", f"{PROXY}/t/3")])
    assert player.book is None
    assert player.state()["track"]["id"] == 3


# --- progress sync --------------------------------------------------------------

def _playing_dune(at_ms: int = 700_000):
    client, player, http = _bus()
    client.dispatch({"type": "play_book", "payload": {"book_id": 7, "position_ms": at_ms}})
    _main(player).time_ms = at_ms
    return client, player, http


def test_push_carries_position_chapter_and_sample_time():
    client, player, http = _playing_dune()
    _main(player).time_ms = 705_000
    client.push_progress()
    path, body = http.puts[-1]
    assert path == "/api/progress/7"
    assert body["position_seconds"] == 705.0
    assert body["chapter_index"] == 1
    assert body["is_finished"] is False
    assert body["client_updated_at"].endswith("+00:00")


def test_no_push_for_an_unmoved_position_or_zero():
    client, player, http = _playing_dune()
    client.push_progress()  # still exactly where play_book put it
    assert http.puts == []
    _main(player).time_ms = 0
    client.push_progress()
    assert http.puts == []


def test_tick_pushes_every_15s_while_playing_and_never_while_idle(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(bus_module.time, "monotonic", lambda: clock[0])
    client, player, http = _playing_dune()
    player._on_playing()

    _main(player).time_ms = 705_000
    clock[0] += 5
    client._progress_tick()
    assert http.puts == []  # not due yet
    clock[0] += 11
    client._progress_tick()
    assert len(http.puts) == 1

    player.pause()
    _main(player).time_ms = 706_000
    client._progress_tick()  # the stop is saved once
    assert len(http.puts) == 2
    for _ in range(10):  # then nothing, however long it sits paused
        clock[0] += 60
        client._progress_tick()
    assert len(http.puts) == 2


def test_idle_renderer_with_no_book_never_pushes():
    client, player, http = _bus()
    player.play_now([QueueItem("track", 3, "Song", "A", f"{PROXY}/t/3")])
    player._on_playing()
    client._progress_tick()
    client.push_progress()
    assert http.puts == []


def test_pause_command_pushes_immediately():
    client, player, http = _playing_dune()
    _main(player).time_ms = 710_000
    client.dispatch({"type": "pause", "payload": {}})
    assert http.puts[-1][1]["position_seconds"] == 710.0


def test_deactivate_saves_then_stops_speaking_for_the_book():
    client, player, http = _playing_dune()
    _main(player).time_ms = 720_000
    client.dispatch({"type": "deactivate", "payload": {}})
    assert http.puts[-1][1]["position_seconds"] == 720.0
    n = len(http.puts)
    _main(player).time_ms = 730_000
    client.push_progress()  # no longer the active player: never writes again
    client._progress_tick()
    assert len(http.puts) == n


def test_a_stale_409_is_swallowed():
    client, player, http = _playing_dune()
    http.put_status = 409
    _main(player).time_ms = 740_000
    client.push_progress()  # must not raise
    assert len(http.puts) == 1


def test_finished_book_is_reported_once():
    client, player, http = _playing_dune()
    player._ended = True  # the M4B (its only part) played to the end
    client._progress_tick()
    client._progress_tick()
    finished = [b for _, b in http.puts if b["is_finished"]]
    assert len(finished) == 1
    assert finished[0]["position_seconds"] == 3600.0


def test_quitting_saves_the_spot():
    client, player, http = _playing_dune()
    _main(player).time_ms = 750_000
    client.stop()
    assert http.puts[-1][1]["position_seconds"] == 750.0


# --- sleep (rider: save BEFORE the fade's pause, never lose the spot) ----------

def test_sleep_fade_saves_the_book_before_pausing_and_keeps_the_position():
    client, player, http = _playing_dune()
    player._on_playing()
    main = _main(player)
    main.time_ms = 760_000

    order: list[str] = []
    real_push = client.push_progress

    def recording_push():
        order.append(f"push paused={'set_pause(1)' in main.calls}")
        real_push()

    player.on_sleep_end = recording_push
    player.bed_play(f"{PROXY}/api/stream/9", 0.0)
    player.sleep_timer(0, 1, bed_fade_to=0.5)

    assert _wait(lambda: not player.sleep_timer_active())
    assert order == ["push paused=False"]  # saved while still playing
    assert "set_pause(1)" in main.calls
    assert http.puts[-1][1]["position_seconds"] == 760.0
    # The book is still loaded at its spot, and the bed kept going.
    assert player.book is not None and player.book_position_ms() == 760_000
    assert player.bed_active() and player.bed_level == pytest.approx(0.5)
    assert main.media.url == f"{PROXY}/api/stream/7"  # never restarted or re-queued


def test_cancelled_sleep_never_calls_the_hook():
    client, player, _ = _playing_dune()
    called = []
    player.on_sleep_end = lambda: called.append(1)
    player.sleep_timer(0.02, 1)
    player.cancel_sleep_timer()
    time.sleep(0.3)
    assert called == []


def test_book_info_chapter_at():
    info = BookInfo(1, "x", 100, [0, 10, 20])
    assert [info.chapter_at(p) for p in (0, 9, 10, 25)] == [0, 0, 1, 2]
