"""#2806 S3: crossfade on the PC renderer.

The outgoing song keeps its own player and fades out while the next one
becomes the main player and fades in (equal power). Track -> track only:
books, clips, streams and a paused player never crossfade. Fake VLC players
stand in for audio so the logic runs with no speakers.
"""
from __future__ import annotations

import time

import pytest

from audiplex_pc.bus import BusClient
from audiplex_pc.player import BookInfo, Player, QueueItem

PROXY = "http://127.0.0.1:5555"


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
def xp():
    """A player on track 1 of [1, 2, 3], 3 s from the end, crossfade 6 s (watcher off)."""
    player = Player()
    player._player = FakeVlc()
    player.play_now(_items(1, 2, 3))
    player.crossfade_ms = 6000
    player._player.length, player._player.at = 200_000, 197_000
    player.incoming = FakeVlc(at=0)
    player._make_player = lambda: player.incoming
    return player


def test_triggers_only_track_to_track_near_the_end(xp):
    assert xp._should_crossfade()
    xp._player.at = 100_000
    assert not xp._should_crossfade()  # not near the end yet
    xp._player.at = 197_000
    xp.queue[1] = _items(-5, kind="clip")[0]
    assert not xp._should_crossfade()  # a DJ clip is next
    xp.queue[1] = _items(2)[0]
    xp._paused = True
    assert not xp._should_crossfade()
    xp._paused = False
    xp.book = BookInfo(7, "Dune", 1000, [0])
    assert not xp._should_crossfade()
    xp.book = None
    xp._player.length, xp._player.at = 10_000, 7_000  # shorter than two fades
    assert not xp._should_crossfade()
    xp.crossfade_ms = 0
    xp._player.length, xp._player.at = 200_000, 197_000
    assert not xp._should_crossfade()


def test_last_song_and_stream_never_crossfade(xp):
    xp._start(2)
    xp._player.length, xp._player.at = 200_000, 197_000
    assert not xp._should_crossfade()  # nothing after it
    xp.play_now(_items(1) + _items(-1, kind="stream"))
    xp._player.length, xp._player.at = 200_000, 197_000
    assert not xp._should_crossfade()


def test_equal_power_ramp_then_the_old_song_is_released(xp):
    old = xp._player
    xp._begin_crossfade()
    assert xp._player is xp.incoming and xp.index == 1 and xp._xfade_old is old
    assert len(old.events.detached) == 3  # its end can't advance the queue twice
    gen = xp._xfade_gen
    assert xp._crossfade_step(gen, 0.5)
    assert old.volumes[-1] == 71 and xp.incoming.volumes[-1] == 71
    assert xp._crossfade_step(gen, 1.0)
    assert old.stopped and old.released and xp._xfade_old is None
    assert xp.incoming.volumes[-1] == 100 and not xp._crossfade_step(gen, 1.0)
    assert xp.state()["track"]["id"] == 2


def test_skip_or_pause_mid_fade_drops_the_outgoing_song(xp):
    old = xp._player
    xp._begin_crossfade()
    xp.skip()
    assert old.stopped and xp._xfade_old is None and xp.index == 2
    assert xp.incoming.volumes[-1] == 100
    xp.crossfade_ms = 6000
    xp._start(1)
    xp._player.length, xp._player.at = 200_000, 197_000
    old2 = xp._player
    xp.incoming = FakeVlc()
    xp._begin_crossfade()
    xp.pause()
    assert old2.stopped and xp._paused


def test_sleep_fade_in_progress_blocks_a_crossfade(xp):
    xp._fade_volume = 0.4
    assert not xp._should_crossfade()


def test_watcher_runs_a_whole_crossfade(xp):
    old = xp._player
    xp.crossfade_ms = 0
    xp._player.length, xp._player.at = 1_000, 800
    xp.set_crossfade(0.3)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not old.released:
        time.sleep(0.05)
    assert old.released and xp.index == 1 and xp._xfade_old is None
    xp.set_crossfade(0)
    assert xp.crossfade_ms == 0


def test_set_crossfade_clamps(xp):
    xp.set_crossfade(99)
    assert xp.crossfade_ms == 12000
    xp.set_crossfade(-3)
    assert xp.crossfade_ms == 0


def test_bus_set_crossfade():
    bus = BusClient("http://127.0.0.1:1", "t", "pc", "PC", Player(), PROXY)
    try:
        assert bus.dispatch({"type": "set_crossfade", "payload": {"seconds": 4}}) == ("ok", "crossfade 4s")
        assert bus.player.crossfade_ms == 4000
        assert bus.dispatch({"type": "set_crossfade", "payload": {}}) == ("bad_payload", "seconds")
        bus.dispatch({"type": "set_crossfade", "payload": {"seconds": 0}})
        assert bus.player.crossfade_ms == 0
    finally:
        bus.stop()

