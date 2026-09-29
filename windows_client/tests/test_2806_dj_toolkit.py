"""#2806: DJ toolkit on the PC renderer: replace_upcoming (queue edits)."""
from __future__ import annotations

import pytest

from audiplex_pc.bus import BusClient
from audiplex_pc.player import BookInfo, Player, QueueItem

PROXY = "http://127.0.0.1:5555"


def _items(*ids: int) -> list[QueueItem]:
    return [QueueItem("track", i, f"t{i}", "a", f"http://x/{i}") for i in ids]


def _ids(player: Player) -> list[int]:
    return [q.id for q in player.queue]


def test_replace_upcoming_keeps_current_and_played():
    player = Player()
    player.play_now(_items(1, 2, 3, 4))
    player._start(1)
    gen = player._gen
    player.replace_upcoming(_items(9, 8))
    assert _ids(player) == [1, 2, 9, 8]
    assert player.index == 1
    assert player._gen == gen  # the current song was not restarted


def test_replace_upcoming_empty_clears_tail():
    player = Player()
    player.play_now(_items(1, 2, 3))
    player.replace_upcoming([])
    assert _ids(player) == [1]


def test_replace_upcoming_on_empty_player_starts():
    player = Player()
    player.replace_upcoming(_items(5, 6))
    assert _ids(player) == [5, 6] and player.index == 0 and player.is_playing()


def test_replace_upcoming_after_queue_ended_starts_next():
    player = Player()
    player.play_now(_items(1))
    player._on_end(player._gen)  # the only song finished
    player.replace_upcoming(_items(7))
    assert player.index == 1 and player.is_playing()


class _Http:
    def get(self, path, params=None):
        tid = int(path.rsplit("/", 1)[-1])

        class R:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {"title": f"t{tid}", "artist_name": "a"}

        return R()


@pytest.fixture
def bus():
    client = BusClient("http://127.0.0.1:1", "t", "pc", "PC", Player(), PROXY)
    client.client = _Http()
    yield client
    client.stop()


def test_bus_replace_upcoming(bus):
    bus.player.play_now(_items(1, 2, 3))
    assert bus.dispatch({"type": "replace_upcoming", "payload": {"track_ids": [4, 5]}}) == ("ok", "")
    assert _ids(bus.player) == [1, 4, 5]


def test_bus_replace_upcoming_refuses_during_book(bus):
    bus.player.play_now(_items(1, 2))
    bus.player.book = BookInfo(7, "Dune", 1000, [0])
    status, _ = bus.dispatch({"type": "replace_upcoming", "payload": {"track_ids": [4]}})
    assert status == "no_music_queue"
    assert _ids(bus.player) == [1, 2]
