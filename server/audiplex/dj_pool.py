"""DJ pool: persistent state, top-up on track changes, cue management (#5470, #5473, #5477, #5495).

Manages a pool of eligible tracks from mix specs, auto-queuing ahead of the
current track. Supports per-lane round-robin selection with starvation prevention,
cue-triggered injections (patter before specific songs), and balance modes
(even / proportional / none distribution across lanes).

Lanes (#5495): Each component of a DJ request (folder, artist, playlist) is a
separate named lane, never flattened. Pool state keeps per-lane eligible ids.
Picker: round-robin by lane (balance=even), PLUS starvation rule: if a lane
hasn't had a pick in N picks (default 6) or M minutes (default 25), it jumps
the line. A lane with no candidates left is marked exhausted and skipped.

Module-level singleton: _pool_instance is shared across routes and the PlaybackBus hook.
"""

import json
import os
import time
from pathlib import Path
from typing import Any, Optional

from audiplex import taste
from audiplex.identity import build_identity_map


def _get_pool_state_path() -> Path:
    """Resolve pool state file path from env or default data directory."""
    explicit = os.environ.get("AUDIPLEX_DJ_POOL_STATE")
    if explicit:
        return Path(explicit)
    # Default: server/data/dj_pool.json
    return Path(__file__).resolve().parent.parent / "data" / "dj_pool.json"


def _load_json_safe(path: Path, default: Any = None) -> Any:
    """Load JSON, return default on error (corrupt file, missing, etc)."""
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json_atomic(path: Path, data: Any) -> None:
    """Write JSON atomically (temp + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    temp.replace(path)


def _lane(track_ids: list[int], prior: Optional[dict] = None) -> dict[str, Any]:
    """One lane record; a zero-track lane is born exhausted (#5495)."""
    ids = [int(t) for t in track_ids]
    prior = prior or {}
    return {
        "track_ids": ids,
        "played_count": prior.get("played_count", 0),
        "last_played_at": prior.get("last_played_at"),
        "exhausted": not ids,
        "zero_on_resolve": not ids,
    }


def lanes_from_ids(lanes: dict[str, list[int]]) -> dict[str, dict]:
    """{label: [track_ids]} -> full lane records (#5495)."""
    return {label: _lane(ids) for label, ids in lanes.items()}


def owner_user_id(db: Any) -> Optional[int]:
    """The DJ owner's user id (settings.dj_owner_username), same lookup the
    owner-scoped playback routes use; None when that user doesn't exist."""
    from audiplex.config import get_settings
    from audiplex.models import User

    owner = db.query(User).filter(User.username == get_settings().dj_owner_username).first()
    return owner.id if owner else None


class DJPool:
    """Persistent DJ pool state: lanes, eligible tracks, top-up + cue logic."""

    def __init__(self, state_file: Path | str | None = None):
        if state_file is None:
            self.state_file = _get_pool_state_path()
        else:
            self.state_file = Path(state_file)
        self.state = _load_json_safe(self.state_file, self._empty_state())
        # Normalize lanes if missing (backward compat)
        if "lanes" not in self.state:
            self.state["lanes"] = {}
        if "starvation_config" not in self.state:
            self.state["starvation_config"] = {
                "check_interval_picks": 6,
                "check_interval_minutes": 25,
            }
        if "round_robin_index" not in self.state:
            self.state["round_robin_index"] = 0

    @staticmethod
    def _empty_state() -> dict[str, Any]:
        """Default pool state."""
        return {
            "active": False,  # #5495: inline pools have no spec_id
            "spec_id": None,
            "eligible_track_ids": [],  # Legacy: kept for backward compat
            "source_labels": {},  # Legacy: track_id -> source label
            "lanes": {},  # New: {lane_name: {track_ids, played_count, last_played_at, exhausted}}
            "balance_mode": "even",
            "ahead": 4,
            "exclude_recent_hours": 12,
            "played_this_session": [],
            "last_topup_at": None,
            "last_topup_current_track_id": None,
            "starvation_config": {
                "check_interval_picks": 6,
                "check_interval_minutes": 25,
            },
            "round_robin_index": 0,  # Current lane in round-robin
            "pending_cues": [],  # {id, trigger, play_track, say, status, held_boundaries}
            "outro": None,  # armed ride-end outro cue (#5515, dj_triggers)
        }

    def _persist(self) -> None:
        """Save state to disk."""
        _write_json_atomic(self.state_file, self.state)

    def set_pool(
        self,
        spec_id: int,
        track_ids: list[int],
        source_labels: dict[int, str],
        lanes: dict[str, dict] | None = None,
        balance: str = "even",
        ahead: int = 4,
        exclude_recent_hours: float = 12,
        played_this_session: list[int] | None = None,
    ) -> dict[str, Any]:
        """Update pool with new eligible tracks and configuration.

        lanes: {lane_name: {track_ids: [...], played_count: 0, last_played_at: None, exhausted: False}}
        """
        self.state.update({
            "active": True,
            "spec_id": spec_id,
            "eligible_track_ids": track_ids,  # Legacy flat list
            "source_labels": source_labels,
            "balance_mode": balance,
            "ahead": ahead,
            "exclude_recent_hours": exclude_recent_hours,
            "played_this_session": played_this_session or [],
            "last_topup_at": None,
            "last_topup_current_track_id": None,
            "round_robin_index": 0,
        })

        # Initialize lanes if provided, otherwise create from legacy data
        if lanes:
            self.state["lanes"] = lanes
        else:
            # Backward compat: create single lane from flat track list
            self.state["lanes"] = {
                "default": {
                    "track_ids": track_ids.copy(),
                    "played_count": 0,
                    "last_played_at": None,
                    "exhausted": False,
                    "zero_on_resolve": False,
                }
            }

        self._persist()

        total_tracks = sum(len(lane.get("track_ids", [])) for lane in self.state["lanes"].values())
        per_lane = {name: len(lane.get("track_ids", [])) for name, lane in self.state["lanes"].items()}

        return {
            "spec_id": spec_id,
            "eligible_count": total_tracks,
            "per_lane_counts": per_lane,
            "ready": True,
        }

    def is_active(self) -> bool:
        """True while a pool is running (spec-backed or inline)."""
        return bool(self.state.get("active") or self.state.get("spec_id"))

    def stop(self) -> bool:
        """Clear pool state. Returns True only if a pool was running (#5495)."""
        was_active = self.is_active()
        # Chime settings are Todd's preference, not session state (#5499).
        chime_settings = self.state.get("chime_settings")
        self.state = self._empty_state()
        if chime_settings:
            self.state["chime_settings"] = chime_settings
        self._persist()
        return was_active

    def status(self) -> dict[str, Any]:
        """Current pool status with per-lane details."""
        now = time.time()
        lane_details = []

        for lane_name, lane_data in self.state.get("lanes", {}).items():
            track_ids = lane_data.get("track_ids", [])
            last_played_at = lane_data.get("last_played_at")
            minutes_since_played = None
            if last_played_at:
                minutes_since_played = round((now - last_played_at) / 60.0, 1)

            lane_details.append({
                "label": lane_name,
                "remaining": len(track_ids),
                "played_count": lane_data.get("played_count", 0),
                "last_played_at": last_played_at,
                "minutes_since_played": minutes_since_played,
                "exhausted": lane_data.get("exhausted", False),
                "zero_on_resolve": lane_data.get("zero_on_resolve", False),
            })

        return {
            "active": self.is_active(),
            "spec_id": self.state.get("spec_id"),
            "eligible_count": sum(d["remaining"] for d in lane_details),
            "balance_mode": self.state.get("balance_mode"),
            "ahead": self.state.get("ahead"),
            "played_this_session_count": len(self.state.get("played_this_session", [])),
            "last_topup_at": self.state.get("last_topup_at"),
            "lanes": lane_details,
            "source_counts": self._count_sources(),  # Backward compat
            "starvation_config": self.state.get("starvation_config", {}),
            "pending_cues": self.get_pending_cues(),
            **self._trigger_status(),
        }

    def _trigger_status(self) -> dict[str, Any]:
        """Cue detail, chimes and the armed outro from the trigger engine (#5480 #5499 #5515)."""
        try:
            from audiplex import dj_triggers

            t = dj_triggers.status(self)
            return {
                "cues": t["cues"],
                "chimes": t["chimes"],
                "chimes_unsupported": t["chimes_unsupported"],
                "outro": t["outro"],
            }
        except Exception as e:
            return {"trigger_status_error": str(e)}

    def _count_sources(self) -> dict[str, int]:
        """Count tracks per source in eligible set (legacy method)."""
        counts = {}
        source_labels = self.state.get("source_labels", {})
        for track_id in self.state.get("eligible_track_ids", []):
            label = source_labels.get(track_id) or source_labels.get(str(track_id)) or "unknown"
            counts[label] = counts.get(label, 0) + 1
        return counts

    def _select_next_lane(self) -> str | None:
        """Select the next lane using round-robin + starvation rule."""
        lanes = self.state.get("lanes", {})
        if not lanes:
            return None

        lane_names = list(lanes.keys())
        if not lane_names:
            return None

        config = self.state.get("starvation_config", {})
        starvation_picks = config.get("check_interval_picks", 6)
        starvation_minutes = config.get("check_interval_minutes", 25)
        now = time.time()

        # Find lanes that are starving (haven't played in N picks or M minutes)
        starving = []
        for name in lane_names:
            lane = lanes[name]
            if lane.get("exhausted"):
                continue

            picks_since = self.state.get("round_robin_index", 0) - lane.get("played_count", 0)
            last_played_at = lane.get("last_played_at")
            minutes_since = None
            if last_played_at:
                minutes_since = (now - last_played_at) / 60.0

            # Starving if: never played, or N picks ago, or M minutes ago
            if picks_since >= starvation_picks or (minutes_since and minutes_since >= starvation_minutes):
                starving.append(name)

        # Prioritize starving lanes
        if starving:
            selected = starving[0]
        else:
            # Round-robin through non-exhausted lanes
            current_idx = self.state.get("round_robin_index", 0) % len(lane_names)
            attempts = 0
            while attempts < len(lane_names):
                candidate = lane_names[current_idx % len(lane_names)]
                if not lanes[candidate].get("exhausted"):
                    selected = candidate
                    break
                current_idx += 1
                attempts += 1
            else:
                # All lanes exhausted
                return None

        return selected

    def resync_lanes(self, lanes: dict[str, list[int]]) -> dict[str, int]:
        """Replace lane track lists after a spec edit, keeping per-lane stats (#5495).

        lanes: {label: [track_ids]} resolved by the caller. A label that keeps
        its name keeps its played_count / last_played_at; new labels start
        fresh; labels no longer present are dropped. Session history, balance
        and ahead are untouched.
        """
        old = self.state.get("lanes", {})
        self.state["lanes"] = {
            label: _lane(ids, old.get(label)) for label, ids in lanes.items()
        }
        self.state["eligible_track_ids"] = [int(t) for ids in lanes.values() for t in ids]
        self.state["source_labels"] = {
            int(t): label for label, ids in lanes.items() for t in ids
        }
        self._persist()
        return {name: len(ids) for name, ids in lanes.items()}

    def top_up(
        self,
        current_track_id: Optional[int],
        upcoming_track_ids: list[int],
        current_played_track_ids: Optional[list[int]] = None,
        cues: Optional[list[dict]] = None,
        match_trigger_fn=None,
        db: Optional[Any] = None,
        owner_id: Optional[int] = None,
        previous_track_id: Optional[int] = None,
    ) -> dict[str, Any]:
        """
        Suggest picks to top up the queue.

        current_track_id: track currently playing (None if nothing loaded)
        upcoming_track_ids: queue from current onward ([0] is the current track)
        current_played_track_ids: ids already in the device queue (never re-picked)
        cues: cue list to match; defaults to this pool's pending_cues
        match_trigger_fn: kept for back-compat; cues are always matched now
        db: SQLAlchemy session for taste.recent_plays_for and identity lookup
        owner_id: whose play history counts as "recent"; looked up from
            settings.dj_owner_username when omitted (#5495)
        previous_track_id: the track that just ended (for track_end cues)

        Returns: {picks, reason, per_lane_details, pending_cues, next_picks_preview}
        """
        if not self.is_active():
            return {"picks": [], "reason": "no pool set", "pending_cues": []}

        # Don't top up if paused, stream (id=-1), or DJ break (id<0)
        if current_track_id is None or current_track_id < 0:
            return {"picks": [], "reason": "paused or stream/break", "pending_cues": []}

        # Count real tracks in upcoming AFTER current (skip current, streams, DJ breaks)
        real_count = sum(
            1 for tid in upcoming_track_ids[1:] if isinstance(tid, int) and tid > 0
        )

        ahead = self.state.get("ahead", 4)

        # Matching cues first (#5495): their play_track leads the picks.
        from audiplex.dj_triggers import is_engine_cue
        from audiplex.trigger_matcher import match_triggers
        if cues is None:
            cues = self.state.setdefault("pending_cues", [])
        # Clip / action cues belong to dj_triggers (held/stale guard, exact
        # boundary placement); only play_track/say-only cues are matched here.
        cues = [c for c in cues if not is_engine_cue(c)]
        events = [{"kind": "track_start", "track_id": current_track_id}]
        if previous_track_id is not None:
            events.append({"kind": "track_end", "track_id": previous_track_id})
        pending_cues = []
        cue_picks: list[int] = []
        for event in events:
            for cue in match_triggers(event, cues):
                cue["done"] = True
                cue["status"] = "fired"
                pending_cues.append({"id": cue.get("id"), "status": "fired", "say": cue.get("say")})
                if cue.get("play_track"):
                    cue_picks.append(int(cue["play_track"]))

        if real_count >= ahead and not cue_picks:
            self.state["last_topup_current_track_id"] = current_track_id
            self._persist()
            return {"picks": [], "reason": f"already {real_count} tracks ahead", "pending_cues": pending_cues}

        # What must not be picked: this session's picks, whatever is already in
        # the device queue, and (by recording identity) recent plays.
        skip_ids = set(self.state.get("played_this_session", []))
        skip_ids.update(t for t in (current_played_track_ids or []) if isinstance(t, int))
        skip_ids.update(t for t in upcoming_track_ids if isinstance(t, int))
        skip_ids.update(cue_picks)
        skip_recordings: set[str] = set()
        identities: dict = {}

        if db is not None:
            try:
                identities = build_identity_map(db)
                if owner_id is None:
                    owner_id = owner_user_id(db)
                window_minutes = float(self.state.get("exclude_recent_hours", 12)) * 60
                if owner_id is not None and window_minutes > 0:
                    for play in taste.recent_plays_for(db, owner_id, window_minutes, identities):
                        skip_recordings.add(play.recording_id)
            except Exception:
                # If taste lookup fails, just skip session-played tracks
                pass

        def recording_of(tid: int) -> Optional[str]:
            ident = identities.get(tid) if identities else None
            return ident.recording_id if ident is not None else None

        # Anything already queued/played/cued blocks its other copies too.
        for tid in skip_ids:
            rec = recording_of(tid)
            if rec is not None:
                skip_recordings.add(rec)

        def eligible(tid: Any) -> bool:
            if not isinstance(tid, int) or tid <= 0 or tid in skip_ids:
                return False
            rec = recording_of(tid)
            return rec is None or rec not in skip_recordings

        picks: list[int] = list(cue_picks)
        to_pick = ahead - real_count

        # Round-robin + starvation; filtered lazily so a pick blocks its own
        # recording's other copies within the same top-up.
        while len(picks) < to_pick:
            selected_lane = self._select_next_lane()
            if not selected_lane:
                break
            lane = self.state["lanes"][selected_lane]
            pick = next((t for t in lane.get("track_ids", []) if eligible(t)), None)
            if pick is None:
                lane["exhausted"] = True
                continue

            picks.append(pick)
            skip_ids.add(pick)
            rec = recording_of(pick)
            if rec is not None:
                skip_recordings.add(rec)

            lane["played_count"] = lane.get("played_count", 0) + 1
            lane["last_played_at"] = time.time()
            self.state["round_robin_index"] = self.state.get("round_robin_index", 0) + 1

        self.state["last_topup_current_track_id"] = current_track_id
        if not picks:
            self._persist()
            return {"picks": [], "reason": "no eligible candidates", "pending_cues": pending_cues}

        # Record picks as played
        self.state.setdefault("played_this_session", []).extend(picks)
        self.state["last_topup_at"] = time.time()
        self._persist()

        lane_details = [
            {
                "label": lane_name,
                "remaining": len(lane_data.get("track_ids", [])),
                "played_count": lane_data.get("played_count", 0),
                "exhausted": lane_data.get("exhausted", False),
            }
            for lane_name, lane_data in self.state.get("lanes", {}).items()
        ]

        return {
            "picks": picks,
            "reason": f"topped up to {ahead} ahead",
            "per_lane_details": lane_details,
            "pending_cues": pending_cues,
            "next_picks_preview": picks[:3],
        }

    def add_cue(
        self,
        cue_id: int,
        trigger_kind: str,
        trigger_track_id: int,
        play_track: Optional[int] = None,
        say: Optional[str] = None,
        clip_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Add a cue to the pending list."""
        cue = {
            "id": cue_id,
            "trigger": {"kind": trigger_kind, "track_id": trigger_track_id},
            "play_track": play_track,
            "say": say,
            "clip_id": clip_id,
            "status": "pending",
            "done": False,
            "held_boundaries": 0,
        }
        self.state.setdefault("pending_cues", []).append(cue)
        self._persist()
        return cue

    def get_pending_cues(self) -> list[dict]:
        """Get all pending cues."""
        return [c for c in self.state.get("pending_cues", []) if not c.get("done")]

    def mark_cue_done(self, cue_id: int) -> bool:
        """Mark a cue as done/fired."""
        for cue in self.state.get("pending_cues", []):
            if cue.get("id") == cue_id:
                cue["done"] = True
                self._persist()
                return True
        return False


# Module-level singleton: shared by routes and PlaybackBus hook
_pool_instance: DJPool | None = None


def get_pool() -> DJPool:
    """Get or create the module-level pool singleton."""
    global _pool_instance
    if _pool_instance is None:
        _pool_instance = DJPool()
    return _pool_instance


def reset_pool_singleton() -> None:
    """Clear the singleton (for testing)."""
    global _pool_instance
    _pool_instance = None
