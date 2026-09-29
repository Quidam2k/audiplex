from __future__ import annotations

import copy
import logging
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any

import httpx

from .player import BookInfo, QueueItem
from .sleep_fade import resolve_url


logger = logging.getLogger(__name__)

# #2680: how often a playing book's position is saved to the server. The
# phone saves every 30 s; the PC is a little tighter so a handoff loses less.
PROGRESS_PUSH_SECONDS = 15.0


class BusClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        device_id: str,
        device_name: str,
        player: Any,
        proxy_base: str,
    ) -> None:
        self.device_id = device_id
        self.device_name = device_name
        self.player = player
        self.proxy_base = proxy_base.rstrip("/")

        self.client = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(10, read=40),
        )

        self.connected = False
        self.last_error: str | None = None

        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._executed: deque[int] = deque(maxlen=200)
        self._executed_set: set[int] = set()

        # #2680: True only while THIS PC is the device playing the book (set by
        # play_book / a book activate, cleared by deactivate or other audio).
        # Progress is only ever pushed while it is set.
        self._book_sync = False
        self._book_lock = threading.Lock()
        self._last_push_at = 0.0
        self._last_pushed: tuple[int, int] | None = None  # (book id, position)
        self._was_playing = False
        player.on_sleep_end = self.push_progress

    def start(self) -> None:
        if any(thread.is_alive() for thread in self._threads):
            return

        self._threads = [
            threading.Thread(
                target=self.command_loop,
                name="audiplex-command-loop",
                daemon=True,
            ),
            threading.Thread(
                target=self.report_loop,
                name="audiplex-report-loop",
                daemon=True,
            ),
        ]
        for thread in self._threads:
            thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.push_progress()  # #2680: quitting mid-book keeps the spot
        try:
            self.client.close()
        except Exception:
            pass

    def command_loop(self) -> None:
        backoff = 2.0

        while not self._stop.is_set():
            try:
                response = self.client.get(
                    "/api/playback/command/next",
                    params={
                        "device_id": self.device_id,
                        "device_name": self.device_name,
                        "device_type": "windows",
                    },
                )

                if response.status_code == 401:
                    self.connected = False
                    self.last_error = "token rejected"
                    logger.error("token rejected")
                    self._stop.wait(60)
                    continue

                if response.status_code == 204:
                    self.connected = True
                    self.last_error = None
                    backoff = 2.0
                    continue

                response.raise_for_status()
                cmd = response.json()

                self.connected = True
                self.last_error = None
                backoff = 2.0

                command_id = int(cmd["id"])
                if command_id in self._executed_set:
                    self._ack(command_id, "ok", "")
                    continue

                self._remember_executed(command_id)

                try:
                    status, detail = self.dispatch(cmd)
                except Exception as exc:
                    detail = repr(exc)[:300]
                    status = "error"
                    self.last_error = detail
                    logger.exception("Playback command failed")
                    self.client_log(
                        "command_failed",
                        detail,
                        level="error",
                        detail={
                            "command_id": command_id,
                            "type": str(cmd.get("type", "")),
                        },
                    )

                self._ack(command_id, status, detail)

            except Exception as exc:
                if self._stop.is_set():
                    break
                self.connected = False
                self.last_error = repr(exc)[:300]
                logger.warning("Playback bus poll failed: %s", self.last_error)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 30.0)

    def dispatch(self, cmd: dict[str, Any]) -> tuple[str, str]:
        command_type = str(cmd.get("type", ""))
        payload = cmd.get("payload") or {}

        if command_type == "play_book":  # #2680
            if payload.get("book_id") is None:
                return "bad_payload", "book_id"
            return self._play_book(
                int(payload["book_id"]),
                payload.get("position_ms"),
                paused=payload.get("playing") is False,
            )

        if command_type in {"play_now", "queue", "play_next", "replace_upcoming"}:  # #2806
            ids = payload.get("track_ids") or []
            items = self._resolve_tracks(ids)

            if command_type == "replace_upcoming":  # #2806: DJ queue edits
                if self.player.book is not None:
                    return "no_music_queue", "an audiobook is playing"  # same code as the phone
                if ids and not items:
                    return "no_tracks", f"none of {ids} resolved"
                self.player.replace_upcoming(items)
                if len(items) < len(ids):
                    return "partial", f"{len(items)}/{len(ids)} resolved"
                return "ok", ""

            if not items:
                return "no_tracks", f"none of {ids} resolved"

            if command_type == "play_now":
                self._end_book_sync()
                self.player.play_now(items)
            elif command_type == "queue":
                self.player.enqueue(items)
            else:
                self.player.play_next(items)

            if len(items) < len(ids):
                return "partial", f"{len(items)}/{len(ids)} resolved"
            return "ok", ""

        if command_type == "deactivate":
            # Playback is moving to another device: stop here and report the
            # exact spot right away; the server hands off once we ack.
            self.player.pause()
            self._end_book_sync()  # a book's last spot is saved before the handoff
            self.report_now()
            return "ok", ""

        if command_type == "activate":
            ids = payload.get("track_ids") or []
            if not ids and payload.get("book_id") is not None:  # #2680
                return self._play_book(
                    int(payload["book_id"]),
                    int(payload.get("position_ms") or 0),
                    paused=not payload.get("playing", True),
                )
            if not ids:
                return "ok", ""  # nothing was playing; we're simply the target now
            items = self._resolve_tracks(ids)
            if not items:
                return "no_tracks", f"none of {ids} resolved"
            resumes = items[0].id == ids[0]
            self._end_book_sync()
            self.player.play_now(
                items,
                start_ms=int(payload.get("position_ms") or 0) if resumes else 0,
                paused=not payload.get("playing", True),
            )
            return "ok", ""

        if command_type == "reorder":
            if payload.get("from_index") is None:
                return "bad_payload", "from_index"
            if payload.get("to_index") is None:
                return "bad_payload", "to_index"
            try:
                self.player.move(
                    payload["from_index"],
                    payload["to_index"],
                )
            except IndexError as exc:
                return "bad_payload", str(exc)
            return "ok", ""

        if command_type == "skip":
            self.player.skip()
            return "ok", ""

        if command_type == "previous":
            self.player.previous()
            return "ok", ""

        if command_type == "pause":
            self.player.pause()
            self.push_progress()  # #2680
            return "ok", ""

        if command_type == "resume":
            self.player.resume()
            return "ok", ""

        if command_type == "seek":
            if payload.get("position_ms") is None:
                return "bad_payload", "position_ms"
            self.player.seek(payload["position_ms"])
            return "ok", ""

        if command_type == "volume":
            if payload.get("volume") is None:
                return "bad_payload", "volume"
            self.player.set_volume(float(payload["volume"]))
            return "ok", ""

        if command_type == "play_stream":
            url = payload.get("url")
            if not url:
                return "bad_payload", "url"
            self._end_book_sync()
            self.player.play_now(
                [
                    QueueItem(
                        kind="stream",
                        id=0,
                        title=payload.get("title") or "Live stream",
                        artist=None,
                        url=url,
                    )
                ]
            )
            return "ok", ""

        if command_type == "announce":
            clip_url = payload.get("clip_url")
            if not clip_url:
                return "bad_payload", "clip_url"
            if payload.get("clip_id") is None:
                return "bad_payload", "clip_id"

            absolute_url = (
                clip_url
                if clip_url.startswith("http")
                else self.proxy_base + clip_url
            )
            self.player.insert_clip(
                QueueItem(
                    kind="clip",
                    id=payload["clip_id"],
                    title=payload.get("title") or "DJ break",
                    artist="DJ",
                    url=absolute_url,
                ),
                play_now=payload.get("mode") == "now",
            )
            return "ok", ""

        # Sleep engine (#3435): same payloads as the phone's DjCommandClient.
        if command_type == "bed_play":
            url = payload.get("url")
            if not url:
                return "bad_payload", "url"
            volume = payload.get("volume")
            self.player.bed_play(
                resolve_url(url, self.proxy_base),
                0.5 if volume is None else float(volume),
            )
            return "ok", ""

        if command_type == "bed_stop":
            self.player.bed_stop()
            return "ok", ""

        if command_type == "bed_volume":
            if payload.get("volume") is None:
                return "bad_payload", "volume"
            self.player.bed_volume(float(payload["volume"]))
            return "ok", ""

        if command_type == "sleep_timer":
            if payload.get("minutes") is None:
                return "bad_payload", "minutes"
            fade_seconds = payload.get("fade_seconds")
            bed_fade_to = payload.get("bed_fade_to")
            self.player.sleep_timer(
                float(payload["minutes"]),
                120 if fade_seconds is None else int(fade_seconds),
                None if bed_fade_to is None else float(bed_fade_to),
            )
            return "ok", ""

        if command_type == "set_crossfade":  # #2806: seconds 0-12, 0 = off
            if payload.get("seconds") is None:
                return "bad_payload", "seconds"
            self.player.set_crossfade(float(payload["seconds"]))
            return "ok", f"crossfade {self.player.crossfade_ms / 1000:g}s"

        if command_type == "cancel_sleep_timer":
            self.player.cancel_sleep_timer()
            return "ok", ""

        return "unknown_type", command_type

    def report_now(self) -> None:
        """Post the current state immediately (best effort)."""
        try:
            self.client.post(
                "/api/playback/state",
                params={"device_id": self.device_id},
                json=self.player.state(),
            ).raise_for_status()
        except Exception as exc:
            logger.warning("Immediate state report failed: %s", repr(exc)[:300])

    def report_loop(self) -> None:
        last_state: dict[str, Any] | None = None
        last_post = 0.0

        while not self._stop.wait(5):
            self._progress_tick()
            try:
                state = self.player.state()
                now = time.monotonic()

                if state != last_state or now - last_post >= 20:
                    response = self.client.post(
                        "/api/playback/state",
                        params={"device_id": self.device_id},
                        json=state,
                    )
                    response.raise_for_status()
                    last_state = copy.deepcopy(state)
                    last_post = time.monotonic()
            except Exception as exc:
                if self._stop.is_set():
                    break
                self.last_error = repr(exc)[:300]
                logger.warning("Playback state report failed: %s", self.last_error)

    # --- audiobooks (#2680) ---------------------------------------------

    def _play_book(
        self, book_id: int, position_ms: Any, paused: bool = False
    ) -> tuple[str, str]:
        try:
            response = self.client.get(f"/api/library/books/{book_id}")
            if response.status_code == 404:
                return "no_book", f"book {book_id} not found"
            response.raise_for_status()
            detail = response.json()
        except Exception as exc:
            return "error", f"book {book_id}: {repr(exc)[:200]}"

        book, parts = self._book_parts(detail)
        if not parts:
            return "no_book", f"book {book_id} has no playable file"

        if position_ms is None:
            position_ms = self._saved_position_ms(book_id)
        position_ms = max(0, min(int(position_ms), max(book.duration_ms - 1000, 0)))

        self._end_book_sync()
        self.player.play_book(book, parts, position_ms, paused=paused)
        with self._book_lock:
            self._book_sync = True
            self._last_pushed = (book_id, position_ms)
            self._last_push_at = time.monotonic()
        return "ok", ""

    def _book_parts(self, detail: dict[str, Any]) -> tuple[BookInfo, list[QueueItem]]:
        """A multi-file book streams each file with its book-global offset; an
        M4B is one part. Chapter starts come from the book's chapter list."""
        book_id = int(detail["id"])
        chapters = sorted(detail.get("chapters") or [], key=lambda c: c.get("start_seconds") or 0)
        starts = [int(float(c.get("start_seconds") or 0) * 1000) for c in chapters] or [0]
        book = BookInfo(
            id=book_id,
            title=detail.get("title"),
            duration_ms=int(float(detail.get("duration_seconds") or 0) * 1000),
            chapter_starts_ms=starts,
        )
        track_urls = detail.get("track_urls") or []
        if not track_urls:
            return book, [
                QueueItem(
                    kind="book_part",
                    id=book_id,
                    title=book.title,
                    artist=detail.get("author"),
                    url=f"{self.proxy_base}/api/stream/{book_id}",
                )
            ]
        # track_urls end in /track/{chapter index}; that chapter's start is the offset.
        start_by_index = {
            int(c["index"]): int(float(c.get("start_seconds") or 0) * 1000)
            for c in chapters
            if c.get("index") is not None
        }
        parts = []
        for url in track_urls:
            try:
                chapter_index = int(url.rstrip("/").rsplit("/", 1)[-1])
            except ValueError:
                continue
            parts.append(
                QueueItem(
                    kind="book_part",
                    id=book_id,
                    title=book.title,
                    artist=detail.get("author"),
                    url=self.proxy_base + url,
                    offset_ms=start_by_index.get(chapter_index, 0),
                )
            )
        return book, parts

    def _saved_position_ms(self, book_id: int) -> int:
        """Where Todd left off on any device; 0 for a new or finished book."""
        try:
            response = self.client.get(f"/api/progress/{book_id}")
            if response.status_code == 404:
                return 0
            response.raise_for_status()
            saved = response.json()
        except Exception as exc:
            logger.warning("Could not read progress for book %s: %s", book_id, repr(exc)[:200])
            return 0
        if saved.get("is_finished"):
            return 0
        return int(float(saved.get("position_seconds") or 0) * 1000)

    def push_progress(self) -> None:
        """Save the book's position now, if this PC is the one playing it."""
        with self._book_lock:
            if not self._book_sync:
                return
            book = self.player.book
            position_ms = self.player.book_position_ms()
            if book is None or position_ms is None:
                return
            finished = self.player.book_finished()
            if finished:
                position_ms = book.duration_ms or position_ms
            # Same 0-guard as the phone: a not-yet-ready player reads 0, and
            # that must never overwrite a real saved spot.
            if position_ms <= 0:
                return
            if self._last_pushed == (book.id, position_ms) and not finished:
                return
            body = {
                "position_seconds": position_ms / 1000.0,
                "chapter_index": book.chapter_at(position_ms),
                "is_finished": finished,
                "client_updated_at": datetime.now(timezone.utc).isoformat(),
            }
            self._last_push_at = time.monotonic()
            self._last_pushed = (book.id, position_ms)
            if finished:
                self._book_sync = False  # said once; nothing more to save
        try:
            response = self.client.put(f"/api/progress/{book.id}", json=body)
            if response.status_code == 409:
                # Another device saved a newer spot after we sampled ours.
                logger.info("Progress push for book %s was stale; kept the newer one", book.id)
                return
            response.raise_for_status()
        except Exception as exc:
            logger.warning("Progress push failed: %s", repr(exc)[:300])

    def _progress_tick(self) -> None:
        """Every 5 s from report_loop: save while playing (every 15 s), and
        once when playback stops. Nothing is sent while idle."""
        playing = self.player.is_playing()
        stopped = self._was_playing and not playing
        self._was_playing = playing
        with self._book_lock:
            due = self._book_sync and (
                stopped
                or self.player.book_finished()
                or (playing and time.monotonic() - self._last_push_at >= PROGRESS_PUSH_SECONDS)
            )
        if due:
            self.push_progress()

    def _end_book_sync(self) -> None:
        """Save a book's last spot, then stop speaking for it."""
        self.push_progress()
        with self._book_lock:
            self._book_sync = False

    def client_log(
        self,
        event: str,
        message: str,
        level: str = "warning",
        detail: dict[str, Any] | None = None,
    ) -> None:
        try:
            response = self.client.post(
                "/api/playback/client-log",
                json={
                    "level": level,
                    "event": event,
                    "message": message,
                    "detail": detail or {},
                    "at": time.time(),
                },
            )
            response.raise_for_status()
        except Exception:
            pass

    def activate(self, device_id: str) -> bool:
        try:
            response = self.client.post(
                f"/api/playback/devices/{device_id}/activate"
            )
            return response.status_code == 200
        except Exception:
            return False

    def devices(self) -> dict[str, Any]:
        try:
            response = self.client.get("/api/playback/devices")
            if response.status_code != 200:
                return {}
            data = response.json()
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _resolve_tracks(self, ids: list[int]) -> list[QueueItem]:
        items: list[QueueItem] = []

        for track_id in ids:
            try:
                response = self.client.get(f"/api/music/tracks/{track_id}")
                response.raise_for_status()
                track = response.json()
                items.append(
                    QueueItem(
                        kind="track",
                        id=track_id,
                        title=track["title"],
                        artist=track.get("artist_name"),
                        url=(
                            f"{self.proxy_base}"
                            f"/api/music/stream/track/{track_id}"
                        ),
                    )
                )
            except Exception as exc:
                logger.warning(
                    "Could not resolve track %s: %s",
                    track_id,
                    repr(exc)[:300],
                )

        return items

    def _remember_executed(self, command_id: int) -> None:
        if command_id in self._executed_set:
            return

        if len(self._executed) == self._executed.maxlen:
            expired = self._executed.popleft()
            self._executed_set.discard(expired)

        self._executed.append(command_id)
        self._executed_set.add(command_id)

    def _ack(self, command_id: int, status: str, detail: str) -> None:
        try:
            response = self.client.post(
                f"/api/playback/command/{command_id}/ack",
                json={"status": status, "detail": detail},
            )
            response.raise_for_status()
        except Exception:
            pass
