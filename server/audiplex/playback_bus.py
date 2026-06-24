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
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Optional


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

    async def enqueue(self, type: str, payload: dict[str, Any]) -> PlaybackCommandRecord:
        self._seq += 1
        rec = PlaybackCommandRecord(
            id=self._seq, type=type, payload=payload, created_at=time.time()
        )
        await self._queue.put(rec)
        return rec

    async def next(self, timeout: float) -> Optional[PlaybackCommandRecord]:
        """Block up to `timeout` seconds for the next command. None on timeout."""
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    def pending(self) -> int:
        return self._queue.qsize()

    def set_state(self, state: dict[str, Any]) -> None:
        self._state = state
        self._state_updated_at = time.time()

    def get_state(self) -> Optional[dict[str, Any]]:
        if self._state is None:
            return None
        return {**self._state, "updated_at": self._state_updated_at}


# Module-level singleton (single-device v1).
bus = PlaybackBus()
