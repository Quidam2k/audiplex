"""#8197: song <-> DJ-clip edge fades on the PC renderer, and the crossfade setting surviving a restart."""
from __future__ import annotations

import json

import pytest

from audiplex_pc.player import BookInfo, Player, QueueItem, clip_edge_level


class _Events:
    def __init__(self):
        self.attached, self.detached = [], []

    def event_attach(self, kind, cb):
        self.attached.append(kind)

    def event_detach(self, kind):
        self.detached.append(kind)


class FakeVlc:
    def __init__(self, length=200_000, at=0):
        self.length, self.at = length, at
        self.volumes: list[int] = []
        self.stopped = self.released = False
        self.events = _Events()

    def event_manager(self):
        return self.events

    def get_length(self):
        return self.length

    def get_time(self):
        return self.at

    def audio_set_volume(self, v):
        self.volumes.append(v)

    def audio_output_set(self, name):
        return 0

    def set_media(self, media):
        pass

    def play(self):
        pass

    def set_pause(self, on):
        pass

    def set_time(self, ms):
        self.at = ms

    def stop(self):
        self.stopped = True

    def release(self):
        self.released = True


def _items(*ids: int, kind: str = "track") -> list[QueueItem]:
    return [QueueItem(kind, i, f"t{i}", "a", f"http://x/{i}") for i in ids]


@pytest.fixture
def player():
    player = Player()
    player._player = FakeVlc()
    player.play_now(_items(1, 2))
    return player


def test_clip_edge_level():
    for prev, cur, nxt, at, length, expected in [
        (False, False, False, 0, 200_000, 1.0),
        (False, False, False, 200_000, 200_000, 1.0),
        (False, False, True, 190_000, 200_000, 1.0),
        (False, False, True, 198_750, 200_000, 0.7071),
        (False, False, True, 200_000, 200_000, 0.0),
        (True, False, False, 0, 200_000, 0.0),
        (True, False, False, 750, 200_000, 0.7071),
        (True, False, False, 1500, 200_000, 1.0),
        (True, False, False, -1, 200_000, 0.0),
        (True, True, True, 0, 200_000, 1.0),
        (False, False, True, 0, 0, 1.0),
        (False, False, True, 0, -1, 1.0),
    ]:
        assert clip_edge_level(prev, cur, nxt, at, length) == pytest.approx(expected, abs=1e-3)


def test_song_fades_before_clip(player):
    clip = QueueItem("clip", -5, "DJ", "DJ", "http://x/c")
    player.insert_clip(clip, play_now=False)
    assert player.queue == [_items(1)[0], clip, _items(2)[0]]
    with player._lock:
        player._player.length, player._player.at = 200_000, 198_750
        player._apply_main_volume()
        assert player._player.volumes[-1] == 71
        player._player.at = 100_000
        player._apply_main_volume()
        assert player._player.volumes[-1] == 100


def test_song_after_clip_fades_in(player):
    player.insert_clip(QueueItem("clip", -5, "DJ", "DJ", "http://x/c"), play_now=False)
    with player._lock:
        player._start(2)
        for at, expected in [(0, 0), (750, 71), (5000, 100)]:
            player._player.at = at
            player._apply_main_volume()
            assert player._player.volumes[-1] == expected


def test_books_and_plain_queues_are_untouched(player):
    player._player.at = 198_750
    player._apply_main_volume()
    assert player._player.volumes[-1] == 100
    player.book = BookInfo(7, "Dune", 200_000, [0])
    player.queue.insert(1, QueueItem("clip", -5, "DJ", "DJ", "http://x/c"))
    player._apply_main_volume()
    assert player._player.volumes[-1] == 100


def test_edge_watcher_exits_without_clips(player):
    player.insert_clip(QueueItem("clip", -5, "DJ", "DJ", "http://x/c"), play_now=False)
    watcher = player._edge_watch
    assert watcher is not None and watcher.is_alive()
    with player._lock:
        player.queue = _items(1)
        player.index = 0
    watcher.join(timeout=2)
    assert not watcher.is_alive()


def test_crossfade_persists(tmp_path):
    path = tmp_path / "settings.json"
    p = Player(settings_path=path)
    p.set_crossfade(4)
    assert json.loads(path.read_text(encoding="utf-8"))["crossfade_seconds"] == 4
    restarted = Player(settings_path=path)
    assert restarted.crossfade_ms == 4000
    restarted.set_crossfade(0)
    assert Player(settings_path=path).crossfade_ms == 0


def test_existing_settings_keys_are_preserved(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"other": 1}', encoding="utf-8")
    Player(settings_path=path).set_crossfade(2)
    assert json.loads(path.read_text(encoding="utf-8")) == {"other": 1, "crossfade_seconds": 2}


def test_corrupt_settings_are_ignored(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("not json", encoding="utf-8")
    assert Player(settings_path=path).crossfade_ms == 0


def test_persist_false_does_not_write_settings(tmp_path):
    path = tmp_path / "settings.json"
    p = Player(settings_path=path)
    p.set_crossfade(4, persist=False)
    assert p.crossfade_ms == 4000
    assert not path.exists()
