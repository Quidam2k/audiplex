"""VLC is stubbed so these tests run without libvlc and never make a sound."""
from __future__ import annotations

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class FakeMedia:
    def __init__(self, url: str) -> None:
        self.url = url
        self.options: list[str] = []

    def add_option(self, option: str) -> None:
        self.options.append(option)


class FakeEventManager:
    def __init__(self) -> None:
        self.handlers: dict[str, list] = {}

    def event_attach(self, event_type, callback) -> None:
        self.handlers.setdefault(event_type, []).append(callback)

    def fire(self, event_type) -> None:
        for callback in self.handlers.get(event_type, []):
            callback(None)


class FakeMediaPlayer:
    def __init__(self) -> None:
        self.events = FakeEventManager()
        self.media: FakeMedia | None = None
        self.volumes: list[int] = []
        self.calls: list[str] = []
        self.released = False
        self.time_ms = 0

    aout: str | None = None

    def audio_output_set(self, name: str) -> int:
        self.aout = name
        return 0

    def event_manager(self) -> FakeEventManager:
        return self.events

    def set_media(self, media: FakeMedia) -> None:
        self.calls.append("set_media")
        self.media = media

    def play(self) -> None:
        self.calls.append("play")

    def stop(self) -> None:
        self.calls.append("stop")

    def release(self) -> None:
        self.released = True

    def set_pause(self, flag: int) -> None:
        self.calls.append(f"set_pause({flag})")

    def audio_set_volume(self, volume: int) -> None:
        self.volumes.append(volume)

    def get_time(self) -> int:
        return self.time_ms

    def get_length(self) -> int:
        return 60_000

    def set_time(self, ms: int) -> None:
        self.time_ms = ms


class FakeInstance:
    def __init__(self, *args: str) -> None:
        self.args = args
        self.players: list[FakeMediaPlayer] = []

    def media_player_new(self) -> FakeMediaPlayer:
        player = FakeMediaPlayer()
        self.players.append(player)
        return player

    def media_new(self, url: str) -> FakeMedia:
        return FakeMedia(url)


fake_vlc = types.ModuleType("vlc")
fake_vlc.Instance = FakeInstance
fake_vlc.MediaPlayer = FakeMediaPlayer
fake_vlc.EventType = types.SimpleNamespace(
    MediaPlayerEndReached="end",
    MediaPlayerEncounteredError="error",
    MediaPlayerPlaying="playing",
)
sys.modules["vlc"] = fake_vlc
