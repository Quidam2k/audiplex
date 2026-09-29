from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

import vlc

from . import sleep_fade


logger = logging.getLogger(__name__)


@dataclass
class QueueItem:
    kind: str
    id: int
    title: str | None
    artist: str | None
    url: str
    offset_ms: int = 0  # #2680: where a book part starts in the whole book


@dataclass
class BookInfo:
    """The audiobook on the main player (#2680). Positions are book-global."""

    id: int
    title: str | None
    duration_ms: int
    chapter_starts_ms: list[int]  # ascending, one per chapter; [0] when unknown

    def chapter_at(self, position_ms: int) -> int:
        index = 0
        for i, start in enumerate(self.chapter_starts_ms):
            if start <= position_ms:
                index = i
        return index


def _reported_id(item: QueueItem) -> int:
    """Catalog id for tracks; -1 for clips/streams, which aren't catalog tracks
    (the phone uses negative ids the same way) and can't follow a transfer."""
    return item.id if item.kind == "track" else -1


class Player:
    def __init__(
        self,
        on_error: Callable[[str, str], None] | None = None,
        vlc_args: Iterable[str] = (),
        bed_aout: str | None = "directsound",
    ) -> None:
        self.on_error = on_error
        # #2680: called (outside the lock) when a sleep fade finishes, BEFORE
        # the main player pauses, so the book's spot is saved first.
        self.on_sleep_end: Callable[[], None] | None = None
        self.book: BookInfo | None = None

        # vlc_args lets a test harness pass e.g. --aout=dummy (no speakers).
        self._instance = vlc.Instance(
            "--no-video",
            "--quiet",
            "--intf=dummy",
            *vlc_args,
        )
        self._player = self._instance.media_player_new()
        self._lock = threading.RLock()

        self.queue: list[QueueItem] = []
        self.index = -1
        self.volume = 1.0

        self._playing = False
        self._paused = False
        self._ended = True
        # Bumped whenever the media changes, so an EndReached/Error for the
        # previous track that lands after a skip can't advance the new one.
        self._gen = 0
        self._pause_on_start = False

        # Sleep engine (#3435): a second, independent looping "bed" player on
        # the same VLC instance (Windows mixes the two outputs), and a sleep
        # timer that fades the MAIN player. The fade drives _fade_volume, an
        # override on top of the configured self.volume, so a track change
        # mid-fade keeps the faded level and resume/cancel restores the real one.
        self._fade_volume: float | None = None
        self._sleep_cancel: threading.Event | None = None
        self._sleep_thread: threading.Thread | None = None
        self._bed: vlc.MediaPlayer | None = None
        self._bed_url: str | None = None
        self.bed_level = 0.0
        self._bed_gen = 0
        # VLC's default Windows output (mmdevice, also wasapi) shares ONE volume
        # across every player in the process, so fading the main player would
        # fade the bed with it. DirectSound gives the bed its own volume.
        self._bed_aout = bed_aout

        # Crossfade (#2806): off by default. When on, the next track starts on a
        # fresh player (DirectSound, so its volume is its own) and the two
        # overlap for crossfade_ms with an equal-power ramp. Only track -> track:
        # books, streams, clips and a paused player never crossfade.
        self.crossfade_ms = 0
        self._xfade_old: vlc.MediaPlayer | None = None
        self._xfade_in: float | None = None  # incoming level, multiplies the main volume
        self._xfade_gen = 0
        self._xfade_watch: threading.Thread | None = None

        self._attach_events(self._player)

    def play_now(
        self, items: Iterable[QueueItem], start_ms: int = 0, paused: bool = False
    ) -> None:
        """Replace the queue. start_ms/paused let a transfer resume mid-track."""
        with self._lock:
            self.book = None
            self._fade_volume = None
            self.queue = list(items)
            if not self.queue:
                self.index = -1
                self._stop_player()
                return
            self._start(0, start_ms)
            self._pause_on_start = paused

    def play_book(
        self, book: BookInfo, parts: list[QueueItem], position_ms: int = 0, paused: bool = False
    ) -> None:
        """Replace the queue with an audiobook's parts (one for an M4B) and
        start at a book-global position (#2680)."""
        with self._lock:
            self.play_now([])
            if not parts:
                return
            self.book = book
            self.queue = list(parts)
            index, local_ms = self._locate(position_ms)
            self._start(index, local_ms)
            self._pause_on_start = paused

    def book_position_ms(self) -> int | None:
        """Book-global position, or None when no book is loaded."""
        with self._lock:
            return self._book_position_unlocked()

    def book_finished(self) -> bool:
        """The last part played to its end."""
        with self._lock:
            return self.book is not None and self._ended and self.index == len(self.queue) - 1

    def is_playing(self) -> bool:
        with self._lock:
            return self._playing

    def enqueue(self, items: Iterable[QueueItem]) -> None:
        with self._lock:
            new_items = list(items)
            if not new_items:
                return

            if not self.queue:
                self.queue.extend(new_items)
                self._start(0)
                return

            first_new_index = len(self.queue)
            ended_at_tail = self._ended and self.index == len(self.queue) - 1
            self.queue.extend(new_items)

            if ended_at_tail:
                self._start(first_new_index)

    def play_next(self, items: Iterable[QueueItem]) -> None:
        with self._lock:
            new_items = list(items)
            if not new_items:
                return

            if not self.queue:
                self.queue.extend(new_items)
                self._start(0)
                return

            insert_at = self.index + 1
            self.queue[insert_at:insert_at] = new_items

    def replace_upcoming(self, items: Iterable[QueueItem]) -> None:  # #2806
        """Replace everything after the current item; the current one plays on."""
        with self._lock:
            new_items = list(items)
            if not self.queue:
                if new_items:
                    self.queue.extend(new_items)
                    self._start(0)
                return
            ended_at_tail = self._ended and self.index == len(self.queue) - 1
            self.queue[self.index + 1:] = new_items
            if ended_at_tail and new_items:
                self._start(self.index + 1)

    def insert_clip(self, item: QueueItem, play_now: bool) -> None:
        with self._lock:
            if not self.queue:
                self.queue.append(item)
                self._start(0)
                return

            insert_at = self.index + 1
            ended_at_tail = self._ended and self.index == len(self.queue) - 1
            self.queue.insert(insert_at, item)

            if play_now or ended_at_tail:
                self._start(insert_at)

    def move(self, from_index: int, to_index: int) -> None:
        with self._lock:
            queue_length = len(self.queue)
            if not 0 <= from_index < queue_length:
                raise IndexError("from_index out of range")
            if not 0 <= to_index < queue_length:
                raise IndexError("to_index out of range")
            if from_index == to_index:
                return

            current_index = self.index
            item = self.queue.pop(from_index)
            self.queue.insert(to_index, item)

            if current_index < 0:
                return
            if from_index == current_index:
                self.index = to_index
            elif from_index < current_index <= to_index:
                self.index -= 1
            elif to_index <= current_index < from_index:
                self.index += 1

    def skip(self) -> None:
        with self._lock:
            if self.book is not None:
                # A book skips by chapter; the last chapter has nowhere to go.
                position = self._book_position_unlocked() or 0
                chapter = self.book.chapter_at(position)
                if chapter + 1 < len(self.book.chapter_starts_ms):
                    self._seek_book(self.book.chapter_starts_ms[chapter + 1])
                return
            if self.index + 1 < len(self.queue):
                self._start(self.index + 1)
            else:
                self._stop_player()

    def previous(self) -> None:
        with self._lock:
            if not self.queue or self.index < 0:
                return

            if self.book is not None:
                position = self._book_position_unlocked() or 0
                chapter = self.book.chapter_at(position)
                start = self.book.chapter_starts_ms[chapter]
                if position - start <= 3000 and chapter > 0:
                    start = self.book.chapter_starts_ms[chapter - 1]
                self._seek_book(start)
                return

            position_ms = max(self._player.get_time(), 0)
            if position_ms > 3000:
                self._player.set_time(0)
                return

            self._start(max(self.index - 1, 0))

    def pause(self) -> None:
        with self._lock:
            if not self.queue or self.index < 0 or self._ended:
                return

            self._kill_xfade()  # #2806: pausing mid-crossfade drops the outgoing song
            self._player.set_pause(1)
            self._playing = False
            self._paused = True

    def resume(self) -> None:
        with self._lock:
            if not self.queue or self.index < 0:
                return

            self._fade_volume = None
            if self._ended:
                self._start(self.index)
                return

            self._paused = False
            self._playing = True
            self._apply_main_volume()
            self._player.play()

    def toggle(self) -> None:
        with self._lock:
            if self._playing:
                self.pause()
            else:
                self.resume()

    def seek(self, position_ms: int) -> None:
        with self._lock:
            if not self.queue or self.index < 0:
                return
            if self.book is not None:
                self._seek_book(int(position_ms))
                return
            self._player.set_time(max(int(position_ms), 0))

    def set_volume(self, volume: float) -> None:
        with self._lock:
            self.volume = max(0.0, min(1.0, float(volume)))
            if self._fade_volume is None:
                self._apply_main_volume()

    def stop(self) -> None:
        with self._lock:
            self._stop_player()

    # --- sleep engine (#3435) -------------------------------------------

    def bed_play(self, url: str, volume: float) -> None:
        """Start (or replace) the looping bed layer. Never touches the main
        player, its queue, or the reported state."""
        with self._lock:
            self._release_bed()
            bed = self._instance.media_player_new()
            if self._bed_aout and bed.audio_output_set(self._bed_aout) != 0:
                logger.warning("Bed output %s unavailable; its volume may follow the main player", self._bed_aout)
            gen = self._bed_gen  # bound per player: a stale bed can't restart a new one
            events = bed.event_manager()
            events.event_attach(
                vlc.EventType.MediaPlayerEndReached,
                lambda _event: self._dispatch_bed_end(gen),
            )
            # The output only exists once playing: re-apply the level then, so
            # a bed meant to start silent never starts at full volume.
            events.event_attach(
                vlc.EventType.MediaPlayerPlaying,
                lambda _event: self._dispatch_bed_playing(gen),
            )
            self._bed = bed
            self._bed_url = url
            self.bed_level = max(0.0, min(1.0, float(volume)))
            self._play_bed_media()

    def bed_stop(self) -> None:
        with self._lock:
            self._release_bed()

    def bed_volume(self, volume: float) -> None:
        with self._lock:
            self._set_bed_level(volume)

    def bed_active(self) -> bool:
        with self._lock:
            return self._bed is not None

    def sleep_timer(
        self, minutes: float, fade_seconds: int = 120, bed_fade_to: float | None = None
    ) -> None:
        """After `minutes`, fade the MAIN player to 0 over `fade_seconds` and
        pause it; whatever is playing is never re-queued or restarted. With
        bed_fade_to the bed ramps up to it in the same loop (phone parity,
        SleepFade.kt). Superseded by a later call or cancel_sleep_timer."""
        with self._lock:
            self._cancel_sleep_thread()
            if self._fade_volume is not None:
                self._fade_volume = None
                self._apply_main_volume()
            cancel = threading.Event()
            self._sleep_cancel = cancel
            self._sleep_thread = threading.Thread(
                target=self._run_sleep_timer,
                args=(cancel, float(minutes), int(fade_seconds), bed_fade_to),
                name="audiplex-sleep-timer",
                daemon=True,
            )
            self._sleep_thread.start()

    def cancel_sleep_timer(self) -> None:
        """Stop a pending/running fade and restore the configured volume. A
        bed still at 0 was only there to be crossfaded into, so it is stopped
        rather than left looping silently all night."""
        with self._lock:
            self._cancel_sleep_thread()
            self._fade_volume = None
            self._apply_main_volume()
            if self._bed is not None and self.bed_level <= 0.0:
                self._release_bed()

    def sleep_timer_active(self) -> bool:
        with self._lock:
            thread = self._sleep_thread
        return thread is not None and thread.is_alive()

    def _run_sleep_timer(
        self,
        cancel: threading.Event,
        minutes: float,
        fade_seconds: int,
        bed_fade_to: float | None,
    ) -> None:
        if cancel.wait(max(minutes * 60.0, 0.0)):
            return
        with self._lock:
            if cancel.is_set():
                return
            main_start = self.volume
            bed_start = self.bed_level if self._bed is not None else 0.0
        steps = max(max(fade_seconds, 1) * 4, 1)
        step_delay = max(fade_seconds / steps, 0.05)
        logger.info(
            "Sleep fade: %ss, main %.2f->0, bed %s", fade_seconds, main_start,
            "untouched" if bed_fade_to is None else f"{bed_start:.2f}->{bed_fade_to:.2f}",
        )
        for i in range(steps + 1):
            main, bed = sleep_fade.levels(i, steps, main_start, bed_start, bed_fade_to)
            with self._lock:
                if cancel.is_set():
                    return
                self._fade_volume = main
                self._apply_main_volume()
                if bed is not None and self._bed is not None:
                    self._set_bed_level(bed)
            if cancel.wait(step_delay):
                return
        # #2680: save the book's spot while it is still there, then pause.
        # Outside the lock: the hook does network I/O.
        if self.on_sleep_end is not None and not cancel.is_set():
            try:
                self.on_sleep_end()
            except Exception:
                logger.exception("Sleep-end hook failed")
        with self._lock:
            if cancel.is_set():
                return
            # _fade_volume stays at 0 until play_now/resume/cancel, so there is
            # no full-volume blip while VLC's async pause lands.
            self.pause()
            if self._sleep_cancel is cancel:
                self._sleep_cancel = None
            bed_note = f"at {self.bed_level:.2f}" if self._bed is not None else "off"
        logger.info("Sleep fade done: main paused, bed %s", bed_note)

    def _cancel_sleep_thread(self) -> None:
        if self._sleep_cancel is not None:
            self._sleep_cancel.set()
        self._sleep_cancel = None

    def _base_level(self) -> float:
        return self.volume if self._fade_volume is None else self._fade_volume

    def _apply_main_volume(self) -> None:
        level = self._base_level() * (1.0 if self._xfade_in is None else self._xfade_in)
        self._player.audio_set_volume(round(level * 100))

    # --- crossfade (#2806) ------------------------------------------------

    def set_crossfade(self, seconds: float) -> None:
        """0 turns it off. Applies from the next track change on."""
        with self._lock:
            self.crossfade_ms = int(max(0.0, min(12.0, float(seconds))) * 1000)
            if self.crossfade_ms and (self._xfade_watch is None or not self._xfade_watch.is_alive()):
                self._xfade_watch = threading.Thread(target=self._watch_crossfade, daemon=True)
                self._xfade_watch.start()

    def _attach_events(self, player: vlc.MediaPlayer) -> None:
        events = player.event_manager()
        events.event_attach(vlc.EventType.MediaPlayerEndReached, self._dispatch_end)
        events.event_attach(vlc.EventType.MediaPlayerEncounteredError, self._dispatch_error)
        events.event_attach(vlc.EventType.MediaPlayerPlaying, self._dispatch_playing)

    def _detach_events(self, player: vlc.MediaPlayer) -> None:
        events = player.event_manager()
        for kind in (vlc.EventType.MediaPlayerEndReached, vlc.EventType.MediaPlayerEncounteredError,
                     vlc.EventType.MediaPlayerPlaying):
            events.event_detach(kind)

    def _make_player(self) -> vlc.MediaPlayer:
        player = self._instance.media_player_new()
        if self._bed_aout and player.audio_output_set(self._bed_aout) != 0:
            logger.warning("Crossfade output %s unavailable; the fade may be uneven", self._bed_aout)
        return player

    def _watch_crossfade(self) -> None:
        while True:
            with self._lock:
                if not self.crossfade_ms:
                    return
                if self._should_crossfade():
                    self._begin_crossfade()
            time.sleep(0.2)

    def _should_crossfade(self) -> bool:
        if (self.book is not None or not self._playing or self._paused or self._ended
                or self._xfade_old is not None or self._fade_volume is not None):
            return False
        nxt = self.index + 1
        if nxt >= len(self.queue):
            return False
        cur, following = self.queue[self.index], self.queue[nxt]
        if cur.kind != "track" or following.kind != "track" or cur.id <= 0 or following.id <= 0:
            return False
        length, at = self._player.get_length(), self._player.get_time()
        if length <= 2 * self.crossfade_ms or at < 0:
            return False
        return length - at <= self.crossfade_ms

    def _begin_crossfade(self) -> None:
        """The current song keeps going on its own player, fading out; the next
        one becomes the main player and fades in."""
        old = self._player
        self._detach_events(old)  # its EndReached must not advance the queue
        self._player = self._make_player()
        self._attach_events(self._player)
        self._start(self.index + 1)
        self._xfade_old = old
        self._xfade_in = 0.0
        self._apply_main_volume()
        self._xfade_gen += 1
        threading.Thread(target=self._run_crossfade, args=(self._xfade_gen, self.crossfade_ms),
                         daemon=True).start()

    def _crossfade_step(self, gen: int, t: float) -> bool:
        """Set both levels at t (0..1) of the ramp. False once superseded."""
        with self._lock:
            if gen != self._xfade_gen or self._xfade_old is None:
                return False
            t = max(0.0, min(1.0, t))
            self._xfade_old.audio_set_volume(round(self._base_level() * math.cos(t * math.pi / 2) * 100))
            self._xfade_in = math.sin(t * math.pi / 2)
            self._apply_main_volume()
            if t >= 1.0:
                self._kill_xfade()
            return True

    def _run_crossfade(self, gen: int, ms: int) -> None:
        start = time.monotonic()
        while self._crossfade_step(gen, (time.monotonic() - start) * 1000 / max(ms, 1)):
            time.sleep(0.05)

    def _kill_xfade(self) -> None:
        """End any overlap now: stop the outgoing song, incoming at full level."""
        old, self._xfade_old = self._xfade_old, None
        if self._xfade_in is not None:
            self._xfade_in = None
            self._apply_main_volume()
        if old is not None:
            self._xfade_gen += 1
            old.stop()
            old.release()

    def _set_bed_level(self, volume: float) -> None:
        self.bed_level = max(0.0, min(1.0, float(volume)))
        if self._bed is not None:
            self._bed.audio_set_volume(round(self.bed_level * 100))

    def _play_bed_media(self) -> None:
        media = self._instance.media_new(self._bed_url)
        # Loop forever; the EndReached restart below is the backstop.
        media.add_option(":input-repeat=65535")
        self._bed.set_media(media)
        self._bed.audio_set_volume(round(self.bed_level * 100))
        self._bed.play()

    def _release_bed(self) -> None:
        self._bed_gen += 1
        bed, self._bed, self._bed_url = self._bed, None, None
        self.bed_level = 0.0
        if bed is not None:
            try:
                bed.stop()
                bed.release()
            except Exception:
                logger.exception("Releasing the bed player failed")

    def _dispatch_bed_end(self, gen: int) -> None:
        threading.Thread(target=self._on_bed_end, args=(gen,), daemon=True).start()

    def _dispatch_bed_playing(self, gen: int) -> None:
        threading.Thread(target=self._on_bed_playing, args=(gen,), daemon=True).start()

    def _on_bed_playing(self, gen: int) -> None:
        with self._lock:
            if gen == self._bed_gen and self._bed is not None:
                self._bed.audio_set_volume(round(self.bed_level * 100))

    def _on_bed_end(self, gen: int) -> None:
        with self._lock:
            if gen != self._bed_gen or self._bed is None:
                return
            logger.info("Bed reached its end; restarting the loop")
            self._play_bed_media()

    def state(self) -> dict[str, object]:
        with self._lock:
            item = self._current_unlocked()
            position_ms = max(self._player.get_time(), 0) if item else 0
            duration_ms = max(self._player.get_length(), 0) if item else 0
            book = None
            if self.book is not None and item is not None:
                position_ms = self._book_position_unlocked() or 0
                duration_ms = self.book.duration_ms or duration_ms
                book = {
                    "id": self.book.id,
                    "title": self.book.title,
                    "chapter_index": self.book.chapter_at(position_ms),
                }
            queue_start = max(self.index, 0)
            queue_end = min(len(self.queue), queue_start + 200)

            track = None
            if item is not None and book is None:
                track = {
                    "id": _reported_id(item),
                    "title": item.title,
                    "artist": item.artist,
                }

            reported_queue = [
                {
                    "index": queue_index,
                    "id": _reported_id(queue_item),
                    "title": queue_item.title,
                    "artist": queue_item.artist,
                }
                for queue_index, queue_item in enumerate(
                    self.queue[queue_start:queue_end],
                    start=queue_start,
                )
            ]

            return {
                "playing": self._playing,
                "track": track,
                "position_ms": position_ms,
                "duration_ms": duration_ms,
                "queue_length": len(self.queue),
                "queue_index": max(self.index, 0),
                "queue": reported_queue,
                "volume": self.volume,
                "book": book,
            }

    def current(self) -> QueueItem | None:
        with self._lock:
            return self._current_unlocked()

    def _book_position_unlocked(self) -> int | None:
        item = self._current_unlocked()
        if self.book is None or item is None:
            return None
        return item.offset_ms + max(self._player.get_time(), 0)

    def _locate(self, position_ms: int) -> tuple[int, int]:
        """(part index, ms into that part) for a book-global position."""
        index = 0
        for i, part in enumerate(self.queue):
            if part.offset_ms <= position_ms:
                index = i
        return index, max(int(position_ms) - self.queue[index].offset_ms, 0)

    def _seek_book(self, position_ms: int) -> None:
        index, local_ms = self._locate(max(int(position_ms), 0))
        if index == self.index and not self._ended:
            self._player.set_time(local_ms)
        else:
            paused = self._paused
            self._start(index, local_ms)
            self._pause_on_start = paused

    def _current_unlocked(self) -> QueueItem | None:
        if 0 <= self.index < len(self.queue):
            return self.queue[self.index]
        return None

    def _start(self, index: int, start_ms: int = 0) -> None:
        self._kill_xfade()  # #2806: a skip/new queue mid-crossfade ends the overlap
        self._gen += 1
        self._pause_on_start = False
        self.index = index
        item = self.queue[index]
        media = self._instance.media_new(item.url)
        if start_ms > 0:
            media.add_option(f":start-time={start_ms / 1000:.3f}")
        self._player.set_media(media)

        self._ended = False
        self._paused = False
        self._playing = True
        self._player.play()

    def _stop_player(self) -> None:
        self._kill_xfade()  # #2806
        self._gen += 1
        self._player.stop()
        self._playing = False
        self._paused = False
        self._ended = True

    def _dispatch_end(self, _event: object) -> None:
        threading.Thread(target=self._on_end, args=(self._gen,), daemon=True).start()

    def _dispatch_error(self, _event: object) -> None:
        threading.Thread(target=self._on_error, args=(self._gen,), daemon=True).start()

    def _dispatch_playing(self, _event: object) -> None:
        threading.Thread(target=self._on_playing, daemon=True).start()

    def _on_end(self, gen: int) -> None:
        with self._lock:
            if gen != self._gen:
                return
            if self.index + 1 < len(self.queue):
                self._start(self.index + 1)
            else:
                self._playing = False
                self._paused = False
                self._ended = True

    def _on_error(self, gen: int) -> None:
        with self._lock:
            if gen != self._gen:
                return
            item = self._current_unlocked()
            if self.index + 1 < len(self.queue):
                self._start(self.index + 1)
            else:
                self._stop_player()

        logger.warning("Playback error on %s", item.url if item else "?")
        # Reported outside the lock: the callback does network I/O.
        if item is not None and self.on_error is not None:
            try:
                self.on_error("player_error", f"{item.title}: {item.url}")
            except Exception:
                logger.exception("Player error callback failed")

    def _on_playing(self) -> None:
        with self._lock:
            self._apply_main_volume()
            if self._pause_on_start:
                # A paused handoff: load at the position, then hold there.
                self._pause_on_start = False
                self._player.set_pause(1)
                self._playing = False
                self._paused = True
            elif not self._paused and not self._ended:
                self._playing = True

