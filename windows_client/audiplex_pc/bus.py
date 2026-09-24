from __future__ import annotations

import copy
import logging
import threading
import time
from collections import deque
from typing import Any

import httpx

from .player import QueueItem


logger = logging.getLogger(__name__)


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

        if command_type in {"play_now", "queue", "play_next"}:
            ids = payload.get("track_ids") or []
            items = self._resolve_tracks(ids)

            if not items:
                return "no_tracks", f"none of {ids} resolved"

            if command_type == "play_now":
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
            self.report_now()
            return "ok", ""

        if command_type == "activate":
            ids = payload.get("track_ids") or []
            if not ids:
                return "ok", ""  # nothing was playing; we're simply the target now
            items = self._resolve_tracks(ids)
            if not items:
                return "no_tracks", f"none of {ids} resolved"
            resumes = items[0].id == ids[0]
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

        if command_type in {
            "bed_play",
            "bed_stop",
            "bed_volume",
            "sleep_timer",
            "cancel_sleep_timer",
        }:
            return "unsupported", "not on the Windows renderer yet"

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
