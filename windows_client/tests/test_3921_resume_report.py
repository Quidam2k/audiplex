"""#3921: the PC posts its state right after a state-changing command, so a
transfer straight after a resume carries playing=true instead of the stale
5 s report. Fake player and fake HTTP only - nothing plays."""
from __future__ import annotations

from audiplex_pc.bus import BusClient

from test_2680_books import Resp


class FakePlayer:
    book = None

    def __init__(self) -> None:
        self.playing = False

    def resume(self) -> None:
        self.playing = True

    def pause(self) -> None:
        self.playing = False

    def state(self) -> dict:
        return {"playing": self.playing}


class OneCommandHttp:
    """Serves one command, then stops the loop; records every POST in order."""

    def __init__(self, bus: BusClient, cmd: dict) -> None:
        self.bus = bus
        self.cmd = cmd
        self.posts: list[tuple[str, dict | None]] = []

    def get(self, path, params=None):
        if self.cmd is None:
            self.bus._stop.set()
            return Resp(204)
        cmd, self.cmd = self.cmd, None
        return Resp(200, cmd)

    def post(self, path, params=None, json=None):
        self.posts.append((path, json))
        return Resp(200, {})


def _run(command_type: str) -> tuple[FakePlayer, list]:
    player = FakePlayer()
    bus = BusClient("http://127.0.0.1:1", "t", "pc", "PC", player, "http://127.0.0.1:2")
    http = OneCommandHttp(bus, {"id": 1, "type": command_type, "payload": {}})
    bus.client = http
    bus.command_loop()
    return player, http.posts


def test_resume_posts_playing_true_right_after_the_ack():
    player, posts = _run("resume")
    assert player.playing
    assert [p for p, _ in posts] == ["/api/playback/command/1/ack", "/api/playback/state"]
    assert posts[1][1] == {"playing": True}


def test_pause_posts_playing_false_after_the_ack():
    _, posts = _run("pause")
    assert posts[-1] == ("/api/playback/state", {"playing": False})


def test_a_command_that_changes_nothing_posts_no_state():
    _, posts = _run("no_such_command")
    assert [p for p, _ in posts] == ["/api/playback/command/1/ack"]
