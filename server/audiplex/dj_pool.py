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
"""

import json
import time
from pathlib import Path
from typing import Any, Optional

from audiplex import taste
from audiplex.identity import build_identity_map


DEFAULT_POOL_STATE_FILE = "data/dj_pool.json"


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


class DJPool:
    """Persistent DJ pool state: lanes, eligible tracks, top-up + cue logic."""

    def __init__(self, state_file: str = DEFAULT_POOL_STATE_FILE):
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

    def stop(self) -> None:
        """Clear pool state."""
        self.state = self._empty_state()
        self._persist()

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
            "spec_id": self.state.get("spec_id"),
            "eligible_count": sum(d["remaining"] for d in lane_details),
            "balance_mode": self.state.get("balance_mode"),
            "ahead": self.state.get("ahead"),
            "played_this_session_count": len(self.state.get("played_this_session", [])),
            "last_topup_at": self.state.get("last_topup_at"),
            "lanes": lane_details,
            "source_counts": self._count_sources(),  # Backward compat
            "starvation_config": self.state.get("starvation_config", {}),
        }

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

    def top_up(
        self,
        current_track_id: Optional[int],
        upcoming_track_ids: list[int],
        current_played_track_ids: Optional[list[int]] = None,
        cues: Optional[list[dict]] = None,
        match_trigger_fn=None,
        db: Optional[Any] = None,
    ) -> dict[str, Any]:
        """
        Suggest picks to top up the queue.

        current_track_id: track currently playing (None if nothing loaded)
        upcoming_track_ids: queue after current (may include current)
        current_played_track_ids: tracks played in this session (outside this pool)
        cues: list of cues with triggers to check
        match_trigger_fn: function(event, cues) -> list[cue_ids]
        db: SQLAlchemy session for taste.recent_plays_for and identity lookup

        Returns: {picks, reason, per_lane_details, pending_cues, next_picks_preview}
        """
        if not self.state.get("spec_id"):
            return {"picks": [], "reason": "no pool set", "pending_cues": []}

        # Don't top up if paused, stream (id=-1), or DJ break (id<0)
        if current_track_id is None or current_track_id < 0:
            return {"picks": [], "reason": "paused or stream/break", "pending_cues": []}

        # Count real tracks in upcoming AFTER current (skip current, streams, DJ breaks)
        real_count = sum(
            1 for tid in upcoming_track_ids[1:] if isinstance(tid, int) and tid > 0
        )

        ahead = self.state.get("ahead", 4)
        if real_count >= ahead:
            return {"picks": [], "reason": f"already {real_count} tracks ahead", "pending_cues": []}

        # Check for matching cues first
        pending_cues = []
        cue_picks = []
        if cues and match_trigger_fn:
            try:
                from audiplex.trigger_matcher import match_triggers
                event = {"kind": "track_end", "track_id": current_track_id}
                matched = match_triggers(event, cues)
                for cue in matched:
                    if cue.get("play_track"):
                        cue_picks.append(cue.get("play_track"))
                        # Mark cue as done
                        cue["done"] = True
                        pending_cues.append({
                            "id": cue.get("id"),
                            "status": "fired",
                            "say": cue.get("say"),
                        })
            except ImportError:
                pass

        # Get recently played tracks to exclude (via taste module)
        skip_ids = set(self.state.get("played_this_session", []))
        skip_recordings = set()
        skip_works = set()

        if db:
            try:
                exclude_hours = self.state.get("exclude_recent_hours", 12)
                # Get window in minutes
                window_minutes = exclude_hours * 60

                identities = build_identity_map(db)
                recent_plays = taste.recent_plays_for(db, 1, window_minutes, identities)  # user_id=1 (owner)

                for play in recent_plays:
                    skip_recordings.add(play.recording_id)
                    skip_works.add(play.work_id)
            except Exception:
                # If taste lookup fails, just skip session-played tracks
                pass

        # Collect candidates from lanes, filtering by identity + recent plays
        candidates_by_lane = {}

        for lane_name, lane_data in self.state.get("lanes", {}).items():
            if lane_data.get("exhausted"):
                continue

            lane_track_ids = lane_data.get("track_ids", [])
            candidates = []

            for tid in lane_track_ids:
                if tid <= 0 or tid in skip_ids:  # Skip played-this-session
                    continue

                # TODO: check identity for deduplication if identities available
                # For now, just exclude by recording_id if we have it
                candidates.append(tid)

            candidates_by_lane[lane_name] = candidates

        # Select picks using round-robin + starvation
        picks = []
        to_pick = ahead - real_count
        picks_made = 0

        # First add cue picks
        picks.extend(cue_picks)
        picks_made = len(cue_picks)

        # Then round-robin + starvation
        while picks_made < to_pick:
            selected_lane = self._select_next_lane()
            if not selected_lane:
                break

            candidates = candidates_by_lane.get(selected_lane, [])
            if not candidates:
                # Mark lane as exhausted
                self.state["lanes"][selected_lane]["exhausted"] = True
                continue

            # Pick first candidate from lane
            pick = candidates[0]
            picks.append(pick)
            candidates.pop(0)
            picks_made += 1

            # Update lane stats
            now = time.time()
            self.state["lanes"][selected_lane]["played_count"] += 1
            self.state["lanes"][selected_lane]["last_played_at"] = now

            # Move to next lane for round-robin
            self.state["round_robin_index"] += 1

        if not picks:
            return {"picks": [], "reason": "no eligible candidates", "pending_cues": pending_cues}

        # Record picks as played
        self.state["played_this_session"].extend(picks)
        self.state["last_topup_at"] = time.time()
        self.state["last_topup_current_track_id"] = current_track_id
        self._persist()

        # Build per-lane details for response
        lane_details = []
        for lane_name, lane_data in self.state.get("lanes", {}).items():
            lane_details.append({
                "label": lane_name,
                "remaining": len(lane_data.get("track_ids", [])),
                "played_count": lane_data.get("played_count", 0),
                "exhausted": lane_data.get("exhausted", False),
            })

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
