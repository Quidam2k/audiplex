"""In-memory playback command bus + now-playing state (DJ remote, v1).

The DJ agent POSTs commands here; the Android client long-polls them and
writes back its now-playing state.

v1 is single-device / single-household (locked decision Q5), so the bus is
GLOBAL — one queue, one state — rather than keyed per user. Any authenticated
caller shares it, so the agent (service-account token) and the device (Todd's
token) reach the same queue without device-targeting. Per-user keying is a
documented future extension.

Cold-start = Option A: a command waits in the queue until the client drains
it, so it survives the client being asleep (it executes on the next wake).
State lives in process memory only and does NOT survive a server restart —
acceptable for v1.

Liveness (#900/#2961): the bus also records WHEN the device last polled, last
took delivery of a command, and last reported state. Without those three
timestamps the DJ tools cannot tell "no player is alive" from "the player is
idle" from "the player reported half an hour ago and has since died" — they all
looked identical, which is exactly what made the 2026-08-14 test fail silently.
A poll is the strongest liveness signal we have: the client re-issues it every
~25s whether or not anything is playing.

Client log (#2961): the app ships player errors and process-exit reasons here
so a silent death on the phone is diagnosable from the server without adb.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, Optional

# How long after the last poll we still consider a device connected. The client
# re-issues its long-poll immediately after each 25s timeout, so anything past
# ~2 missed cycles means it is gone, not slow.
DEVICE_STALE_AFTER_SECONDS = 60.0

CLIENT_LOG_CAPACITY = 200


@dataclass
class PlaybackCommandRecord:
    id: int
    type: str
    payload: dict[str, Any]
    created_at: float


class PlaybackBus:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[PlaybackCommandRecord] = asyncio.Queue()
        self._seq: int = 0
        self._state: Optional[dict[str, Any]] = None
        self._state_updated_at: float = 0.0
        self._last_poll_at: float = 0.0
        self._last_delivered_at: float = 0.0
        self._last_delivered: Optional[PlaybackCommandRecord] = None
        self._client_log: Deque[dict[str, Any]] = deque(maxlen=CLIENT_LOG_CAPACITY)
        self._log_seq: int = 0

    def reset(self) -> None:
        """Drop all state — used by tests, which share this global singleton."""
        PlaybackBus.__init__(self)

    async def enqueue(self, type: str, payload: dict[str, Any]) -> PlaybackCommandRecord:
        self._seq += 1
        rec = PlaybackCommandRecord(
            id=self._seq, type=type, payload=payload, created_at=time.time()
        )
        await self._queue.put(rec)
        return rec

    async def next(self, timeout: float) -> Optional[PlaybackCommandRecord]:
        """Block up to `timeout` seconds for the next command. None on timeout.

        Records the poll itself as a liveness beat: a client that times out
        empty-handed has still proven it is alive and listening.
        """
        self._last_poll_at = time.time()
        try:
            rec = await asyncio.wait_for(self._queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        self._last_delivered = rec
        self._last_delivered_at = time.time()
        return rec

    def pending(self) -> int:
        return self._queue.qsize()

    def set_state(self, state: dict[str, Any]) -> None:
        self._state = state
        self._state_updated_at = time.time()

    def get_state(self) -> Optional[dict[str, Any]]:
        if self._state is None:
            return None
        return {**self._state, "updated_at": self._state_updated_at}

    def device_status(self) -> dict[str, Any]:
        """Everything the DJ needs to tell a dead player from a quiet one."""
        now = time.time()

        def age(ts: float) -> Optional[float]:
            return round(now - ts, 1) if ts else None

        poll_age = age(self._last_poll_at)
        return {
            "connected": poll_age is not None and poll_age < DEVICE_STALE_AFTER_SECONDS,
            "last_poll_at": self._last_poll_at or None,
            "last_poll_age_seconds": poll_age,
            "last_state_at": self._state_updated_at or None,
            "last_state_age_seconds": age(self._state_updated_at),
            "ever_reported_state": self._state is not None,
            "last_command_id": self._last_delivered.id if self._last_delivered else None,
            "last_command_type": self._last_delivered.type if self._last_delivered else None,
            "last_command_delivered_at": self._last_delivered_at or None,
            "last_command_delivered_age_seconds": age(self._last_delivered_at),
            "pending": self.pending(),
        }

    def add_client_log(self, entry: dict[str, Any]) -> dict[str, Any]:
        """Append a client-shipped log entry. Server clock is authoritative for
        ordering; the client's own timestamp is kept alongside it."""
        self._log_seq += 1
        record = {**entry, "id": self._log_seq, "received_at": time.time()}
        self._client_log.append(record)
        return record

    def client_log(self, limit: int = 50) -> list[dict[str, Any]]:
        """Most recent entries last, oldest first — reads like a log file."""
        entries = list(self._client_log)
        return entries[-limit:] if limit > 0 else entries


# Module-level singleton (single-device v1).
bus = PlaybackBus()
