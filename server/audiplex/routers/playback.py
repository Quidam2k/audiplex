"""DJ playback command bus + now-playing state (remote control, v1).

Endpoints:
  POST /api/playback/command       — agent enqueues a command
  GET  /api/playback/command/next  — client long-polls for the next command
  POST /api/playback/state         — client reports now-playing
  GET  /api/playback/state         — agent reads now-playing

All require a valid Bearer token (get_current_user). v1 is single-device, so
the bus is global: agent and device share one queue + one state regardless of
which account's token they present (see playback_bus for the rationale).

Transport = long-poll (locked decision): the GET blocks up to ~25s (under the
client's 30s read timeout) and returns 204 on timeout, at which point the
client simply re-issues.
"""

from fastapi import APIRouter, Depends, Response
from fastapi.responses import JSONResponse

from audiplex.auth import get_current_user
from audiplex.models import User
from audiplex.playback_bus import bus
from audiplex.schemas import PlaybackCommand, PlaybackCommandQueued, PlaybackState

router = APIRouter(prefix="/api/playback", tags=["playback"])

LONGPOLL_TIMEOUT_SECONDS = 25.0


@router.post("/command", response_model=PlaybackCommandQueued)
async def post_command(cmd: PlaybackCommand, user: User = Depends(get_current_user)):
    rec = await bus.enqueue(cmd.type, cmd.payload)
    return PlaybackCommandQueued(id=rec.id, type=rec.type, pending=bus.pending())


@router.get("/command/next")
async def next_command(user: User = Depends(get_current_user)):
    """Long-poll for the next command. 204 (empty) on timeout — re-issue."""
    rec = await bus.next(LONGPOLL_TIMEOUT_SECONDS)
    if rec is None:
        return Response(status_code=204)
    return JSONResponse(
        {
            "id": rec.id,
            "type": rec.type,
            "payload": rec.payload,
            "created_at": rec.created_at,
        }
    )


@router.post("/state", response_model=PlaybackState)
def post_state(state: PlaybackState, user: User = Depends(get_current_user)):
    bus.set_state(state.model_dump())
    return state


@router.get("/state")
def get_state(user: User = Depends(get_current_user)):
    return bus.get_state() or {
        "playing": False,
        "track": None,
        "position_ms": 0,
        "duration_ms": 0,
        "queue_length": 0,
        "queue_index": 0,
        "queue": [],
        "updated_at": None,
    }
