"""#3921: media keys against a faked winrt - never touches the real SMTC, so no OS overlay."""
import importlib
import sys
from types import ModuleType, SimpleNamespace

import pytest


class SystemMediaTransportControlsButton:
    PLAY, PAUSE, NEXT, PREVIOUS, STOP = range(5)


class MediaPlaybackStatus:
    PLAYING, PAUSED, STOPPED = range(3)


class MediaPlaybackType:
    MUSIC = 1


class FakeUpdater:
    def __init__(self):
        self.type = None
        self.music_properties = SimpleNamespace(title="", artist="")
        self.calls = 0

    def update(self):
        self.calls += 1


class FakeSMTC:
    def __init__(self):
        for name in ("enabled", "play_enabled", "pause_enabled",
                     "next_enabled", "previous_enabled", "stop_enabled"):
            setattr(self, "is_" + name, False)
        self.playback_status = None
        self.display_updater = FakeUpdater()
        self.handler = None
        self.token = object()
        self.removed = []

    def add_button_pressed(self, handler):
        self.handler = handler
        return self.token

    def remove_button_pressed(self, token):
        self.removed.append(token)
        self.handler = None


class FakeMediaPlayer:
    instances = []

    def __init__(self):
        self.command_manager = SimpleNamespace(is_enabled=True)
        self.system_media_transport_controls = FakeSMTC()
        self.instances.append(self)


class FakePlayer:
    def __init__(self):
        self.calls = []
        self.current = {"playing": False, "track": None, "book": None}

    def state(self):
        return self.current

    def toggle(self): self.calls.append("toggle")
    def skip(self): self.calls.append("skip")
    def previous(self): self.calls.append("previous")
    def pause(self): self.calls.append("pause")
    def resume(self): self.calls.append("resume")


@pytest.fixture
def media(monkeypatch):
    names = ("winrt", "winrt.windows", "winrt.windows.media",
             "winrt.windows.media.playback")
    modules = {name: ModuleType(name) for name in names}
    for name, module in modules.items():
        module.__path__ = []
        monkeypatch.setitem(sys.modules, name, module)
        if "." in name:
            parent, child = name.rsplit(".", 1)
            setattr(modules[parent], child, module)
    for enum in (MediaPlaybackStatus, MediaPlaybackType,
                 SystemMediaTransportControlsButton):
        setattr(modules["winrt.windows.media"], enum.__name__, enum)
    modules["winrt.windows.media.playback"].MediaPlayer = FakeMediaPlayer
    monkeypatch.setattr(FakeMediaPlayer, "instances", [])
    module = importlib.reload(importlib.import_module("audiplex_pc.media_keys"))
    player, actions = FakePlayer(), []
    keys = module.MediaKeys(player, on_action=lambda: actions.append("action"))
    result = keys.start()
    mp = FakeMediaPlayer.instances[-1]
    return SimpleNamespace(module=module, player=player, actions=actions,
                           keys=keys, result=result, mp=mp,
                           smtc=mp.system_media_transport_controls)


def test_start_enables_controls(media):
    assert media.module.AVAILABLE
    assert media.result is True
    assert media.mp.command_manager.is_enabled is False
    for name in ("enabled", "play_enabled", "pause_enabled",
                 "next_enabled", "previous_enabled"):
        assert getattr(media.smtc, "is_" + name) is True
    assert callable(media.smtc.handler)


@pytest.mark.parametrize("button,method", [
    ("PLAY", "resume"), ("PAUSE", "pause"),
    ("NEXT", "skip"), ("PREVIOUS", "previous"),
])
def test_buttons_call_player_and_callback(media, button, method):
    value = getattr(SystemMediaTransportControlsButton, button)
    media.smtc.handler(None, SimpleNamespace(button=value))
    assert media.player.calls == [method]
    assert media.actions == ["action"]


def test_unknown_button_does_nothing(media):
    media.smtc.handler(None, SimpleNamespace(
        button=SystemMediaTransportControlsButton.STOP))
    assert media.player.calls == []
    assert media.actions == []


def test_player_exception_does_not_escape(media, monkeypatch):
    def fail():
        raise RuntimeError("player failed")
    monkeypatch.setattr(media.player, "resume", fail)
    media.smtc.handler(None, SimpleNamespace(
        button=SystemMediaTransportControlsButton.PLAY))


@pytest.mark.parametrize("playing,track,book,expected", [
    (True, {"title": "Song"}, None, MediaPlaybackStatus.PLAYING),
    (False, {"title": "Song"}, None, MediaPlaybackStatus.PAUSED),
    (False, None, {"title": "Book"}, MediaPlaybackStatus.PAUSED),
    (False, None, None, MediaPlaybackStatus.STOPPED),
])
def test_playback_status(media, playing, track, book, expected):
    media.player.current.update(playing=playing, track=track, book=book)
    media.keys.update()
    assert media.smtc.playback_status == expected


@pytest.mark.parametrize("track,book,title,artist", [
    ({"title": "Song", "artist": "Singer"}, None, "Song", "Singer"),
    (None, {"title": "Book"}, "Book", "Audiplex"),
])
def test_metadata_and_unchanged_update(media, track, book, title, artist):
    media.player.current.update(track=track, book=book)
    media.keys.update()
    updater = media.smtc.display_updater
    assert updater.type == MediaPlaybackType.MUSIC
    assert (updater.music_properties.title,
            updater.music_properties.artist) == (title, artist)
    calls = updater.calls
    assert calls > 0
    media.keys.update()
    assert updater.calls == calls


def test_stop_removes_handler_and_disables_controls(media):
    media.keys.stop()
    assert media.smtc.removed == [media.smtc.token]
    assert media.smtc.is_enabled is False


def test_missing_winrt_is_harmless(media, monkeypatch):
    monkeypatch.setitem(sys.modules, "winrt", None)
    monkeypatch.setitem(sys.modules, "winrt.windows.media", None)
    module = importlib.reload(media.module)
    assert module.AVAILABLE is False
    keys = module.MediaKeys(FakePlayer())
    count = len(FakeMediaPlayer.instances)
    assert keys.start() is False
    keys.update()
    keys.stop()
    assert len(FakeMediaPlayer.instances) == count
