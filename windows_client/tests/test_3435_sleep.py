"""#3435: sleep engine on the PC renderer (bed layer + sleep_timer)."""
from __future__ import annotations

import time

import pytest

from audiplex_pc import sleep_fade
from audiplex_pc.bus import BusClient
from audiplex_pc.player import Player, QueueItem

PROXY = "http://127.0.0.1:5555"


def _wait(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _playing_player() -> Player:
    player = Player()
    player.play_now([QueueItem("track", 1, "Book", None, f"{PROXY}/t/1")])
    return player


def _main(player: Player):
    return player._instance.players[0]


def _beds(player: Player):
    return player._instance.players[1:]


# --- levels(): parity with android SleepFadeTest.kt ------------------------

def test_main_fades_from_start_to_zero():
    assert sleep_fade.levels(0, 8, 0.8, 0, None)[0] == pytest.approx(0.8)
    assert sleep_fade.levels(4, 8, 0.8, 0, None)[0] == pytest.approx(0.4)
    assert sleep_fade.levels(8, 8, 0.8, 0, None)[0] == pytest.approx(0.0)


def test_bed_untouched_without_target():
    assert sleep_fade.levels(4, 8, 1, 0.5, None)[1] is None


def test_bed_crossfades_up_as_main_goes_down():
    last = -1.0
    for i in range(11):
        main, bed = sleep_fade.levels(i, 10, 1, 0, 0.5)
        assert bed >= last
        last = bed
        assert main == pytest.approx(1 - i / 10)
    assert last == pytest.approx(0.5)


def test_bed_target_is_clamped():
    assert sleep_fade.levels(1, 1, 1, 0, 3)[1] == pytest.approx(1.0)


def test_zero_steps_does_not_divide_by_zero():
    assert sleep_fade.levels(0, 0, 1, 0, 0.5) == pytest.approx((1.0, 0.0))


def test_resolve_url_keeps_absolute_and_joins_relative():
    assert sleep_fade.resolve_url("http://x:1/a.mp3", PROXY) == "http://x:1/a.mp3"
    assert sleep_fade.resolve_url("/api/stream/7", PROXY + "/") == f"{PROXY}/api/stream/7"
    assert sleep_fade.resolve_url("api/stream/7", PROXY) == f"{PROXY}/api/stream/7"


# --- bed layer --------------------------------------------------------------

def test_bed_is_a_second_looping_player_that_leaves_main_alone():
    player = _playing_player()
    main_calls = list(_main(player).calls)
    player.bed_play(f"{PROXY}/api/stream/9", 0.4)

    (bed,) = _beds(player)
    assert bed.media.url == f"{PROXY}/api/stream/9"
    assert ":input-repeat=65535" in bed.media.options
    assert bed.volumes[-1] == 40 and "play" in bed.calls
    assert _main(player).calls == main_calls  # main never touched
    assert player.state()["queue_length"] == 1  # bed isn't in the queue


def test_bed_gets_its_own_output_so_its_volume_is_independent():
    # mmdevice/wasapi share one volume per process; DirectSound doesn't.
    player = _playing_player()
    player.bed_play("a", 0.3)
    assert _beds(player)[0].aout == "directsound"
    assert _main(player).aout is None


def test_bed_level_is_reapplied_once_playing():
    # A crossfade bed starts at 0: it must never come up at VLC's default 100.
    player = _playing_player()
    player.bed_play("a", 0.0)
    bed = _beds(player)[0]
    bed.volumes.append(100)  # what an output opened at default would report
    bed.events.fire("playing")
    assert _wait(lambda: bed.volumes[-1] == 0)


def test_bed_replace_and_stop_leave_no_orphan_player():
    player = _playing_player()
    player.bed_play("a", 0.5)
    player.bed_play("b", 0.5)
    first, second = _beds(player)
    assert first.released and not second.released
    player.bed_stop()
    assert second.released and not player.bed_active()


def test_bed_restarts_on_end_but_a_released_bed_does_not():
    player = _playing_player()
    player.bed_play("a", 0.5)
    first = _beds(player)[0]
    first.events.fire("end")
    assert _wait(lambda: first.calls.count("play") == 2)

    player.bed_play("b", 0.5)
    second = _beds(player)[1]
    first.events.fire("end")  # stale event from the replaced bed
    time.sleep(0.1)
    assert second.calls.count("play") == 1


def test_bed_volume_is_clamped():
    player = _playing_player()
    player.bed_play("a", 5)
    assert player.bed_level == 1.0
    player.bed_volume(-1)
    assert _beds(player)[0].volumes[-1] == 0


# --- sleep timer -----------------------------------------------------------

def test_crossfade_ramps_main_down_and_bed_up_then_pauses():
    player = _playing_player()
    player.set_volume(0.8)
    player.bed_play("bed", 0.0)
    main, bed = _main(player), _beds(player)[0]
    main_before, bed_before = len(main.volumes), len(bed.volumes)
    calls_before = list(main.calls)

    player.sleep_timer(0, fade_seconds=1, bed_fade_to=0.5)
    assert _wait(lambda: not player.sleep_timer_active())

    ramp = main.volumes[main_before:]
    bed_ramp = bed.volumes[bed_before:]
    assert ramp[0] == 80 and ramp[-1] == 0
    assert ramp == sorted(ramp, reverse=True)
    assert bed_ramp[0] == 0 and bed_ramp[-1] == 50
    assert bed_ramp == sorted(bed_ramp)
    assert len(ramp) == len(bed_ramp) == 5  # same loop, 4 steps/s + endpoint
    # Never re-queued or restarted: only a pause was added.
    assert main.calls == calls_before + ["set_pause(1)"]
    assert player.volume == 0.8  # configured volume kept for resume
    # The bed loops on at its target: one live bed, nothing orphaned.
    assert player.bed_active() and not bed.released
    assert [b for b in _beds(player) if not b.released] == [bed]


def test_fade_without_bed_target_leaves_bed_alone():
    player = _playing_player()
    player.bed_play("bed", 0.3)
    bed = _beds(player)[0]
    before = list(bed.volumes)
    player.sleep_timer(0, fade_seconds=1)
    assert _wait(lambda: not player.sleep_timer_active())
    assert bed.volumes == before
    assert _main(player).volumes[-1] == 0


def test_track_change_mid_fade_keeps_faded_level_and_resume_restores():
    player = _playing_player()
    player._fade_volume = 0.25
    player._on_playing()
    assert _main(player).volumes[-1] == 25
    player.resume()
    assert _main(player).volumes[-1] == 100


def test_cancel_restores_volume_and_stops_a_silent_crossfade_bed():
    player = _playing_player()
    player.bed_play("bed", 0.0)
    player.sleep_timer(10, fade_seconds=1, bed_fade_to=0.5)
    assert player.sleep_timer_active()
    player.cancel_sleep_timer()
    assert _wait(lambda: not player.sleep_timer_active())
    assert _main(player).volumes[-1] == 100
    assert not player.bed_active() and _beds(player)[0].released


def test_cancel_mid_fade_stops_the_ramp_and_keeps_an_audible_bed():
    player = _playing_player()
    player.bed_play("bed", 0.2)
    bed = _beds(player)[0]
    player.sleep_timer(0, fade_seconds=2, bed_fade_to=0.6)
    assert _wait(lambda: len(bed.volumes) >= 3)
    player.cancel_sleep_timer()
    assert _wait(lambda: not player.sleep_timer_active())
    frozen = list(bed.volumes)
    time.sleep(0.6)
    assert bed.volumes == frozen  # ramp no longer drives the bed
    assert player.bed_active()
    assert "set_pause(1)" not in _main(player).calls


def test_later_timer_supersedes_earlier():
    player = _playing_player()
    player.sleep_timer(0, fade_seconds=2)
    time.sleep(0.3)
    player.sleep_timer(10, fade_seconds=1)
    time.sleep(0.8)
    assert _main(player).volumes[-1] == 100  # restored, first ramp dead
    player.cancel_sleep_timer()


# --- bus dispatch -----------------------------------------------------------

@pytest.fixture
def bus():
    client = BusClient("http://127.0.0.1:1", "t", "pc", "PC", _playing_player(), PROXY)
    yield client
    client.player.cancel_sleep_timer()
    client.stop()


def test_bus_bed_play_resolves_relative_url_through_proxy(bus):
    assert bus.dispatch({"type": "bed_play", "payload": {"url": "/api/stream/12", "volume": 0}}) == ("ok", "")
    assert _beds(bus.player)[0].media.url == f"{PROXY}/api/stream/12"
    assert bus.player.bed_level == 0.0


def test_bus_bed_play_defaults_volume(bus):
    bus.dispatch({"type": "bed_play", "payload": {"url": "http://x/b.mp3"}})
    assert bus.player.bed_level == 0.5


def test_bus_bad_payloads(bus):
    assert bus.dispatch({"type": "bed_play", "payload": {}}) == ("bad_payload", "url")
    assert bus.dispatch({"type": "bed_volume", "payload": {}}) == ("bad_payload", "volume")
    assert bus.dispatch({"type": "sleep_timer", "payload": {}}) == ("bad_payload", "minutes")


def test_bus_sleep_timer_and_cancel(bus, monkeypatch):
    seen = []
    monkeypatch.setattr(bus.player, "sleep_timer", lambda *a: seen.append(a))
    bus.dispatch({"type": "sleep_timer", "payload": {"minutes": 30, "fade_seconds": 60, "bed_fade_to": 0.5}})
    bus.dispatch({"type": "sleep_timer", "payload": {"minutes": 1}})
    assert seen == [(30.0, 60, 0.5), (1.0, 120, None)]
    assert bus.dispatch({"type": "cancel_sleep_timer"}) == ("ok", "")


def test_bus_bed_volume_and_stop(bus):
    bus.dispatch({"type": "bed_play", "payload": {"url": "a"}})
    assert bus.dispatch({"type": "bed_volume", "payload": {"volume": 0.7}}) == ("ok", "")
    assert bus.player.bed_level == 0.7
    assert bus.dispatch({"type": "bed_stop"}) == ("ok", "")
    assert not bus.player.bed_active()
