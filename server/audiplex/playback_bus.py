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

Process-exit entries are ALSO appended to disk (#3021). The ring buffer is
memory-only, and the phone advances its own report watermark as soon as the
server takes an entry — so a restart between delivery and a human reading it
destroys that death report permanently: the phone will never re-send it and
Android eventually rolls the record out of its own history. Everything else in
the log is reconstructible; a process-exit report is not, which is why it is
the one category that gets persisted.

Command registry + ACK (#900 Phase 3a). Commands used to live in an
asyncio.Queue, which DESTROYED the record on delivery: `next()` popped it
before the HTTP response was even written, so a client that disconnected
mid-response lost the command silently and the DJ could never say more than
"queued, hopefully". Commands now live in a bounded registry with a status
(queued → delivered → acked/failed) and the queue is gone. Delivery marks;
it does not consume. An un-acked delivery is retried after
REDELIVER_AFTER_SECONDS, so the semantics are at-least-once and the CLIENT
dedupes by command id — losing a command is unrecoverable, repeating one is
noise (ruling on plan-back #9245 Q2).

Link history (#3031). `_last_poll_at` alone is a single float in process
memory, which meant "did the link hold overnight?" was unanswerable even
after the FGS shipped — the 08-18 morning check could see two point samples
and nothing in between. Polls are now kept in a bounded in-memory ring, and
GAPS are appended to disk, because a gap is the only part worth surviving a
restart. A server restart deliberately does NOT record a gap (the device did
nothing wrong); it records a resume marker instead, so an auditor can tell
"continuous" from "we simply weren't watching".
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Deque, Optional

# How long after the last poll we still consider a device connected. The client
# re-issues its long-poll immediately after each 25s timeout, so anything past
# ~2 missed cycles means it is gone, not slow.
DEVICE_STALE_AFTER_SECONDS = 60.0

CLIENT_LOG_CAPACITY = 200

# An un-acked delivery is re-offered after this long. 60s is comfortably past
# a normal dispatch (which acks in well under a second) and past one 25s
# long-poll cycle, so a healthy device is never handed the same command twice.
REDELIVER_AFTER_SECONDS = 60.0

# Commands kept for status lookup after they finish. Bounded so a long-lived
# server cannot grow this without limit; the DJ only ever asks about recent ones.
COMMAND_HISTORY_CAPACITY = 200

# ~24h of polls at the client's ~25s cadence. In memory only — the durable
# artifact is the gap log, since a gap is the thing worth auditing later.
POLL_HISTORY_CAPACITY = 3600

# Two missed long-poll cycles. Past this the device was gone, not slow.
LINK_GAP_THRESHOLD_SECONDS = 120.0

# Append-only JSONL of process-exit reports. Overridable so tests never touch
# the real file.
EXIT_LOG_PATH = Path(
    os.environ.get("AUDIPLEX_EXIT_LOG")
    or Path(__file__).resolve().parent.parent / "data" / "client-exits.jsonl"
)

# Append-only JSONL of link gaps and resumes. Overridable so tests never touch
# the real file.
LINK_LOG_PATH = Path(
    os.environ.get("AUDIPLEX_LINK_LOG")
    or Path(__file__).resolve().parent.parent / "data" / "link-history.jsonl"
)


# Command lifecycle. `failed` is a terminal ACK too: the device told us it
# could not carry the command out, which is a FAR better answer than silence
# and is exactly what the 2026-08-14 test lacked.
STATUS_QUEUED = "queued"
STATUS_DELIVERED = "delivered"
STATUS_ACKED = "acked"
STATUS_FAILED = "failed"

TERMINAL_STATUSES = (STATUS_ACKED, STATUS_FAILED)


@dataclass
class PlaybackCommandRecord:
    id: int
    type: str
    payload: dict[str, Any]
    created_at: float
    status: str = STATUS_QUEUED
    delivered_at: float = 0.0
    delivery_count: int = 0
    acked_at: float = 0.0
    ack_status: Optional[str] = None
    ack_detail: str = ""

    def summary(self) -> dict[str, Any]:
        """What the DJ tools render — the whole point of the registry."""
        return {
            "id": self.id,
            "type": self.type,
            "status": self.status,
            "created_at": self.created_at,
            "delivered_at": self.delivered_at or None,
            "delivery_count": self.delivery_count,
            "acked_at": self.acked_at or None,
            "ack_status": self.ack_status,
            "ack_detail": self.ack_detail,
        }


class PlaybackBus:
    def __init__(self) -> None:
        self._commands: "OrderedDict[int, PlaybackCommandRecord]" = OrderedDict()
        self._arrival: Optional[asyncio.Event] = None
        self._seq: int = 0
        self._state: Optional[dict[str, Any]] = None
        self._state_updated_at: float = 0.0
        self._last_poll_at: float = 0.0
        self._last_delivered_at: float = 0.0
        self._last_delivered: Optional[PlaybackCommandRecord] = None
        self._client_log: Deque[dict[str, Any]] = deque(maxlen=CLIENT_LOG_CAPACITY)
        self._log_seq: int = 0
        self._poll_history: Deque[float] = deque(maxlen=POLL_HISTORY_CAPACITY)
        self._seen_poll_this_process: bool = False

    def reset(self) -> None:
        """Drop all state — used by tests, which share this global singleton."""
        PlaybackBus.__init__(self)

    def _event(self) -> asyncio.Event:
        """The "a command arrived" signal, created lazily.

        Built on first use rather than in __init__ because the singleton is
        constructed at import time, before any event loop exists — binding an
        Event to the wrong loop is how this class would hang under a test that
        spins up its own.
        """
        if self._arrival is None:
            self._arrival = asyncio.Event()
        return self._arrival

    async def enqueue(self, type: str, payload: dict[str, Any]) -> PlaybackCommandRecord:
        self._seq += 1
        rec = PlaybackCommandRecord(
            id=self._seq, type=type, payload=payload, created_at=time.time()
        )
        self._commands[rec.id] = rec
        while len(self._commands) > COMMAND_HISTORY_CAPACITY:
            self._commands.popitem(last=False)
        self._event().set()
        return rec

    def _claim(self, now: float) -> Optional[PlaybackCommandRecord]:
        """The oldest command owed to the device, or None.

        Owed means never delivered, or delivered long enough ago that the ack
        should have come back and did not.
        """
        for rec in self._commands.values():
            if rec.status == STATUS_QUEUED or (
                rec.status == STATUS_DELIVERED
                and now - rec.delivered_at >= REDELIVER_AFTER_SECONDS
            ):
                rec.status = STATUS_DELIVERED
                rec.delivered_at = now
                rec.delivery_count += 1
                self._last_delivered = rec
                self._last_delivered_at = now
                return rec
        return None

    def _next_redelivery_in(self, now: float) -> Optional[float]:
        """Seconds until the earliest un-acked delivery becomes re-offerable."""
        waits = [
            REDELIVER_AFTER_SECONDS - (now - rec.delivered_at)
            for rec in self._commands.values()
            if rec.status == STATUS_DELIVERED
        ]
        return min(waits) if waits else None

    async def next(self, timeout: float) -> Optional[PlaybackCommandRecord]:
        """Block up to `timeout` seconds for the next command. None on timeout.

        Records the poll itself as a liveness beat: a client that times out
        empty-handed has still proven it is alive and listening.

        Delivery MARKS the record, it does not consume it — the command stays
        in the registry until the device acks, so a client that dies between
        this response and dispatch is offered it again instead of losing it.
        """
        self._record_poll()
        deadline = time.time() + timeout
        while True:
            # Clear before claiming, so an enqueue racing this loop re-sets the
            # event and the wait below returns at once instead of parking.
            self._event().clear()
            now = time.time()
            rec = self._claim(now)
            if rec is not None:
                return rec
            remaining = deadline - now
            if remaining <= 0:
                return None
            redeliver_in = self._next_redelivery_in(now)
            wait = remaining if redeliver_in is None else min(remaining, redeliver_in)
            try:
                await asyncio.wait_for(self._event().wait(), timeout=max(wait, 0.0))
            except asyncio.TimeoutError:
                pass

    def ack(
        self, command_id: int, status: str, detail: str = ""
    ) -> Optional[PlaybackCommandRecord]:
        """Record the device's outcome for a command. None if we never had it.

        `status` is the device's word for what happened: 'ok' when it acted,
        anything else (no_tracks, error, ...) when it could not. Either way the
        command leaves the outstanding set — a command the device explicitly
        failed must not be redelivered forever.
        """
        rec = self._commands.get(command_id)
        if rec is None:
            return None
        rec.status = STATUS_ACKED if status == "ok" else STATUS_FAILED
        rec.ack_status = status
        rec.ack_detail = detail
        rec.acked_at = time.time()
        return rec

    def command(self, command_id: int) -> Optional[PlaybackCommandRecord]:
        return self._commands.get(command_id)

    def commands(self, limit: int = 20) -> list[dict[str, Any]]:
        """Recent commands, oldest first — reads like the log it is."""
        records = list(self._commands.values())
        return [r.summary() for r in (records[-limit:] if limit > 0 else records)]

    def pending(self) -> int:
        """Commands never yet handed to the device."""
        return sum(1 for r in self._commands.values() if r.status == STATUS_QUEUED)

    def outstanding(self) -> int:
        """Commands the device has not finished with — queued OR un-acked.

        Distinct from pending() on purpose: a delivered-but-un-acked command is
        exactly the state that used to be invisible, and it is the one worth
        alarming on.
        """
        return sum(
            1 for r in self._commands.values() if r.status not in TERMINAL_STATUSES
        )

    def _record_poll(self) -> None:
        """Log the liveness beat, and any gap that preceded it.

        Only gaps and resumes reach disk. Writing every poll would be ~3.5k
        lines a day to say "still fine"; the question anyone actually asks
        later is "when was it NOT fine".
        """
        now = time.time()
        previous = self._last_poll_at
        self._last_poll_at = now
        self._poll_history.append(now)
        if not self._seen_poll_this_process:
            self._seen_poll_this_process = True
            # A restart is OUR discontinuity, not the device's. Recording it as
            # a gap would manufacture link failures out of every deploy.
            _append_link_event({"event": "resumed", "at": now, "after_restart": True})
            return
        if previous and now - previous > LINK_GAP_THRESHOLD_SECONDS:
            _append_link_event(
                {
                    "event": "gap",
                    "from": previous,
                    "to": now,
                    "seconds": round(now - previous, 1),
                }
            )

    def poll_history(self, limit: int = 200) -> list[float]:
        """Recent poll timestamps, oldest first (in-memory, this process only)."""
        entries = list(self._poll_history)
        return entries[-limit:] if limit > 0 else entries

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
        last = self._last_delivered
        return {
            "connected": poll_age is not None and poll_age < DEVICE_STALE_AFTER_SECONDS,
            "last_poll_at": self._last_poll_at or None,
            "last_poll_age_seconds": poll_age,
            "last_state_at": self._state_updated_at or None,
            "last_state_age_seconds": age(self._state_updated_at),
            "ever_reported_state": self._state is not None,
            "last_command_id": last.id if last else None,
            "last_command_type": last.type if last else None,
            "last_command_delivered_at": self._last_delivered_at or None,
            "last_command_delivered_age_seconds": age(self._last_delivered_at),
            # The ack is the answer to "did it actually land", which is the
            # question the 2026-08-14 test could not ask (#900 Phase 3a).
            "last_command_status": last.status if last else None,
            "last_command_ack_status": last.ack_status if last else None,
            "last_command_ack_detail": last.ack_detail if last else "",
            "pending": self.pending(),
            "outstanding": self.outstanding(),
            "polls_recorded": len(self._poll_history),
        }

    def add_client_log(self, entry: dict[str, Any]) -> dict[str, Any]:
        """Append a client-shipped log entry. Server clock is authoritative for
        ordering; the client's own timestamp is kept alongside it."""
        self._log_seq += 1
        record = {**entry, "id": self._log_seq, "received_at": time.time()}
        self._client_log.append(record)
        if record.get("event") == "process_exit":
            _persist_exit(record)
        return record

    def client_log(self, limit: int = 50) -> list[dict[str, Any]]:
        """Most recent entries last, oldest first — reads like a log file."""
        entries = list(self._client_log)
        return entries[-limit:] if limit > 0 else entries


def _persist_exit(record: dict[str, Any]) -> None:
    """Append one process-exit report to the on-disk log.

    Best-effort by design: this runs inside the request that accepts the
    report, and a disk problem must not turn into a 500 that makes the phone
    treat the entry as undelivered. Losing the durable copy is bad; losing the
    entry AND the phone's watermark position is worse.
    """
    try:
        EXIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(EXIT_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except OSError:
        pass


def _append_link_event(record: dict[str, Any]) -> None:
    """Append one link gap/resume to disk.

    Best-effort like the exit log: this runs inside the device's long-poll, and
    a disk problem must never turn the liveness beat into a 500.
    """
    try:
        LINK_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LINK_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except OSError:
        pass


def read_link_history(limit: int = 50) -> list[dict[str, Any]]:
    """Link gaps and resumes that survived a restart, oldest first.

    This is the durable answer to "did the link actually hold overnight" — a
    question that was unanswerable on 2026-08-18 because the only record was a
    single in-memory float (#3031).
    """
    try:
        with open(LINK_LOG_PATH, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in lines[-limit:] if limit > 0 else lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def read_persisted_exits(limit: int = 50) -> list[dict[str, Any]]:
    """Process-exit reports that survived a restart, oldest first."""
    try:
        with open(EXIT_LOG_PATH, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in lines[-limit:] if limit > 0 else lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


# Module-level singleton (single-device v1).
bus = PlaybackBus()
