"""Ride-end learning (#4057): Pantheon's bike sweep posts the ride window here."""

from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from audiplex import dj_learn
from audiplex.auth import get_current_user
from audiplex.database import get_db
from audiplex.models import DjTrackWeight, User
from audiplex.routers.playback import _resolve_owner

router = APIRouter(prefix="/api/dj", tags=["dj_learn"])


class LearnRequest(BaseModel):
    since: datetime
    until: datetime
    ride_id: str | None = None  # defaults to the window start; replaying one is a no-op


@router.post("/learn")
def learn_from_ride(body: LearnRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Apply one closed ride's lessons; returns the 3-line "what I learned" note."""
    ride_id = body.ride_id or body.since.isoformat()
    return dj_learn.learn(db, _resolve_owner(db).id, body.since, body.until, ride_id)


@router.get("/weights")
def list_weights(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Every learned weight, lowest first, so dropped tracks lead."""
    rows = db.query(DjTrackWeight).order_by(DjTrackWeight.weight, DjTrackWeight.track_id).all()
    return [{"track_id": r.track_id, "weight": r.weight, "reason": r.reason, "ride_id": r.ride_id,
             "skip_rides": r.skip_rides} for r in rows]
