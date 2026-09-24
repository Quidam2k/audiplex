from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass

import vlc


logger = logging.getLogger(__name__)


@dataclass
class QueueItem:
    kind: str
    id: int
    title: str | None
    artist: str | None
    url: str


def _reported_id(item: QueueItem) -> int:
    """Catalog id for tracks; -1 for clips/streams, which aren't catalog tracks
    (the phone uses negative ids the same way) and can't follow a transfer."""
    return item.id if item.kind == "track" else -1


class Player:
    def __init__(
        self,
        on_error: Callable[[str, str], None] | None = None,
    ) -> None:
        self.on_error = on_error

        self._instance = vlc.Instance(
            "--no-video",
            "--quiet",
            "--intf=dummy",
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

        self._event_manager = self._player.event_manager()
        self._event_manager.event_attach(
            vlc.EventType.MediaPlayerEndReached,
            self._dispatch_end,
        )
        self._event_manager.event_attach(
            vlc.EventType.MediaPlayerEncounteredError,
            self._dispatch_error,
        )
        self._event_manager.event_attach(
            vlc.EventType.MediaPlayerPlaying,
            self._dispatch_playing,
        )

    def play_now(
        self, items: Iterable[QueueItem], start_ms: int = 0, paused: bool = False
    ) -> None:
        """Replace the queue. start_ms/paused let a transfer resume mid-track."""
        with self._lock:
            self.queue = list(items)
            if not self.queue:
                self.index = -1
                self._stop_player()
                return
            self._start(0, start_ms)
            self._pause_on_start = paused

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
            if self.index + 1 < len(self.queue):
                self._start(self.index + 1)
            else:
                self._stop_player()

    def previous(self) -> None:
        with self._lock:
            if not self.queue or self.index < 0:
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

            self._player.set_pause(1)
            self._playing = False
            self._paused = True

    def resume(self) -> None:
        with self._lock:
            if not self.queue or self.index < 0:
                return

            if self._ended:
                self._start(self.index)
                return

            self._paused = False
            self._playing = True
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
            self._player.set_time(max(int(position_ms), 0))

    def set_volume(self, volume: float) -> None:
        with self._lock:
            self.volume = max(0.0, min(1.0, float(volume)))
            self._player.audio_set_volume(round(self.volume * 100))

    def stop(self) -> None:
        with self._lock:
            self._stop_player()

    def state(self) -> dict[str, object]:
        with self._lock:
            item = self._current_unlocked()
            position_ms = max(self._player.get_time(), 0) if item else 0
            duration_ms = max(self._player.get_length(), 0) if item else 0
            queue_start = max(self.index, 0)
            queue_end = min(len(self.queue), queue_start + 200)

            track = None
            if item is not None:
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
            }

    def current(self) -> QueueItem | None:
        with self._lock:
            return self._current_unlocked()

    def _current_unlocked(self) -> QueueItem | None:
        if 0 <= self.index < len(self.queue):
            return self.queue[self.index]
        return None

    def _start(self, index: int, start_ms: int = 0) -> None:
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
            self._player.audio_set_volume(round(self.volume * 100))
            if self._pause_on_start:
                # A paused handoff: load at the position, then hold there.
                self._pause_on_start = False
                self._player.set_pause(1)
                self._playing = False
                self._paused = True
            elif not self._paused and not self._ended:
                self._playing = True

