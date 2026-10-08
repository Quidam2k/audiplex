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
import random
import time
from pathlib import Path
from typing import Any, Optional

from audiplex import taste
from audiplex.identity import build_identity_map

# #7108: a lane that has played all its tracks recycles them, but never one
# picked within the last this-many picks (about two hours of songs).
NO_REPEAT_PICKS = 30


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


def _lane(track_ids: list[int], prior: Optional[dict] = None,
          dropped: Optional[dict[str, int]] = None) -> dict[str, Any]:
    """One lane record; a zero-track lane is born exhausted (#5495).

    dropped (#7108): {reason: count} of resolved tracks the server filtered
    out at set time, so a lane with nothing left can say why.
    """
    ids = [int(t) for t in track_ids]
    prior = prior or {}
    dropped = {k: v for k, v in (dropped or {}).items() if v}
    return {
        "dropped": dropped,
        "why_empty": None if ids else _why_no_tracks(dropped),  # #7108
        "track_ids": ids,
        "played_count": prior.get("played_count", 0),
        "last_played_at": prior.get("last_played_at"),
        "exhausted": not ids,
        "zero_on_resolve": not ids,
        "paused": prior.get("paused", False),  # #2806: survives a spec resync
    }


def _why_no_tracks(dropped: Optional[dict[str, int]]) -> str:
    """Why a lane has no tracks at all (#7108)."""
    if dropped:
        parts = ", ".join(f"{n} {why}" for why, n in dropped.items())
        return f"all {sum(dropped.values())} resolved track(s) were filtered out ({parts})"
    return "the source resolved 0 tracks"


def lanes_from_ids(lanes: dict[str, list[int]],
                   dropped: Optional[dict[str, dict[str, int]]] = None) -> dict[str, dict]:
    """{label: [track_ids]} -> full lane records (#5495); dropped per label (#7108)."""
    return {label: _lane(ids, dropped=(dropped or {}).get(label)) for label, ids in lanes.items()}


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
            "refill_at": None,  # #3644: top up only below this many (None = ahead)
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
        refill_at: int | None = None,
        no_repeat_picks: int | None = None,
    ) -> dict[str, Any]:
        """Update pool with new eligible tracks and configuration.

        ahead: how deep the device queue is filled after the current song.
        refill_at: top up only once fewer than this many are left (#3644: a
            deep queue Todd can see, refilled in batches); None = ahead, i.e.
            the old keep-it-exactly-ahead-deep behaviour.
        no_repeat_picks: a played-out lane recycles, but never a track picked
            within this many picks (#7108); None = NO_REPEAT_PICKS.

        lanes: {lane_name: {track_ids: [...], played_count: 0, last_played_at: None, exhausted: False}}
        """
        self.state.update({
            "active": True,
            "spec_id": spec_id,
            "eligible_track_ids": track_ids,  # Legacy flat list
            "source_labels": source_labels,
            "balance_mode": balance,
            "ahead": ahead,
            "refill_at": refill_at,
            "no_repeat_picks": NO_REPEAT_PICKS if no_repeat_picks is None else int(no_repeat_picks),
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
                "paused": lane_data.get("paused", False),  # #2806
                "why_empty": lane_data.get("why_empty"),  # #7108
            })

        return {
            "active": self.is_active(),
            "spec_id": self.state.get("spec_id"),
            "eligible_count": sum(d["remaining"] for d in lane_details),
            "balance_mode": self.state.get("balance_mode"),
            "replace_reason": self.state.get("replace_reason"),  # #4054
            "ahead": self.state.get("ahead"),
            "refill_at": self.state.get("refill_at"),
            "no_repeat_picks": self.state.get("no_repeat_picks", NO_REPEAT_PICKS),  # #7108
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
        """#4054 strict rotation: the active lane picked least recently goes next, so
        no lane repeats until every other active lane has had a turn. Paused and
        exhausted (no eligible track this top-up) lanes are skipped, never waited on.
        Replaces round-robin-by-index + starvation, which could hand the lane after
        a skipped one two turns in a row; least-recent-first is also starvation-free."""
        lanes = self.state.get("lanes", {})
        order = list(lanes.keys())
        active = [n for n in order if not lanes[n].get("exhausted") and not lanes[n].get("paused")]  # #2806
        if not active:
            return None
        return min(active, key=lambda n: (lanes[n].get("last_pick_index", -1), order.index(n)))

    def set_lane(self, lane: str, action: str) -> dict[str, Any]:  # #2806
        """Pause, resume or remove one lane. Raises KeyError/ValueError with a sayable message."""
        if action not in ("pause", "resume", "remove"):
            raise ValueError(f"Unknown action '{action}'. Use pause, resume or remove.")
        lanes = self.state.get("lanes", {})
        q = lane.strip().lower()
        name = next((n for n in lanes if n.lower() == q), None) or next(
            (n for n in lanes if q and n.lower().startswith(q)), None)
        if name is None:
            raise KeyError(f"No lane '{lane}'. Lanes: {', '.join(lanes) or 'none'}.")
        if action == "remove":
            gone = set(int(t) for t in lanes.pop(name).get("track_ids", []))
            self.state["eligible_track_ids"] = [
                t for t in self.state.get("eligible_track_ids", []) if int(t) not in gone]
        else:
            lanes[name]["paused"] = action == "pause"
        self._persist()
        return {"lane": name, "action": action, "lanes": self.status()["lanes"]}

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

        refill_at = min(int(self.state.get("refill_at") or ahead), ahead)  # #3644
        if real_count >= refill_at and not cue_picks:
            self.state["last_topup_current_track_id"] = current_track_id
            self._persist()
            return {"picks": [], "reason": f"already {real_count} tracks ahead", "pending_cues": pending_cues}
        if real_count >= refill_at:
            to_fill = 0  # #3644: only the cue's own track goes in
        else:
            to_fill = ahead - real_count

        # What must not be picked: this session's picks, whatever is already in
        # the device queue, and (by recording identity) recent plays.
        skip_ids = set(self.state.get("played_this_session", []))
        skip_ids.update(t for t in (current_played_track_ids or []) if isinstance(t, int))
        skip_ids.update(t for t in upcoming_track_ids if isinstance(t, int))
        skip_ids.update(cue_picks)
        skip_recordings: set[str] = set()
        identities: dict = {}
        banned: set[int] = set()
        played_before: set[str] = set()  # #7108: owner plays inside exclude_recent_hours

        if db is not None:
            try:
                identities = build_identity_map(db)
                from audiplex.dj_bans import banned_ids  # #2806

                banned = set(banned_ids(db, identities))
                skip_ids.update(banned)
                if owner_id is None:
                    owner_id = owner_user_id(db)
                window_minutes = float(self.state.get("exclude_recent_hours", 12)) * 60
                if owner_id is not None and window_minutes > 0:
                    for play in taste.recent_plays_for(db, owner_id, window_minutes, identities):
                        played_before.add(play.recording_id)
                skip_recordings |= played_before
            except Exception as e:  # #7335: logged, still fail-open for picking
                # If taste lookup fails, just skip session-played tracks
                print(f"[dj_pool] WARNING taste/ban lookup failed, picking without it: {e}", flush=True)  # #7335

        def recording_of(tid: int) -> Optional[str]:
            ident = identities.get(tid) if identities else None
            return ident.recording_id if ident is not None else None

        # Anything already queued/played/cued blocks its other copies too.
        for tid in skip_ids:
            rec = recording_of(tid)
            if rec is not None:
                skip_recordings.add(rec)

        # #7335: one song, any version. A work already queued, playing, cued, or  # #7335
        # counted inside the work cooldown is not picked, and a pick blocks its  # #7335
        # work for the rest of this top-up. Checked BEFORE a pick is taken, so a  # #7335
        # rejected candidate is never consumed from its lane.  # #7335
        work_of = {tid: ident.work_id for tid, ident in identities.items()} if identities else {}  # #7335
        blocked_works: set[str] = {  # #7335
            work_of[t] for t in [current_track_id, *upcoming_track_ids, *(current_played_track_ids or [])]  # #7335
            if isinstance(t, int) and t in work_of  # #7335
        }  # #7335
        if db is not None:  # #7335
            try:  # #7335
                from audiplex import queue_guard  # #7335
                counted = queue_guard.load_guard_input(  # #7335
                    db, op="queue", incoming=[], current_id=current_track_id,  # #7335
                    upcoming=[t for t in upcoming_track_ids if isinstance(t, int)], reserved=[],  # #7335
                    owner_id=owner_id, now=time.time(),  # #7335
                )  # #7335
                blocked_works |= queue_guard.work_blocked_keys(counted.work, counted.plays, time.time())  # #7335
            except Exception as e:  # #7335
                print(f"[dj_pool] WARNING work cooldown lookup failed, picking without it: {e}", flush=True)  # #7335
        blocked_works |= {work_of[t] for t in cue_picks if t in work_of}  # #7335

        def work_ok(tid: Any) -> bool:  # #7335
            return work_of.get(tid) not in blocked_works  # #7335

        def eligible(tid: Any) -> bool:
            if not isinstance(tid, int) or tid <= 0 or tid in skip_ids or not work_ok(tid):  # #7335
                return False
            rec = recording_of(tid)
            return rec is None or rec not in skip_recordings

        picks: list[int] = list(cue_picks)
        to_pick = max(len(cue_picks), to_fill)

        # #7108: the order picks will play in, oldest first, for the no-repeat
        # window a recycled pick must stay out of.
        window = int(self.state.get("no_repeat_picks", NO_REPEAT_PICKS))
        history = list(self.state.get("played_this_session", []))
        seen = set(history)
        history += [t for t in [*(current_played_track_ids or []), *upcoming_track_ids]
                    if isinstance(t, int) and t not in seen]

        # A recycled pick may repeat this pool's own songs (outside the window)
        # but not a recording Todd heard recently from somewhere else.
        played_before -= {r for r in map(recording_of, history) if r is not None}

        def recent_window() -> set[int]:
            return set((history + picks)[-window:]) if window > 0 else set()

        def recyclable(tid: Any, recent: set[int], recent_recs: set[str]) -> bool:
            if not isinstance(tid, int) or tid <= 0 or tid in banned or tid in recent or not work_ok(tid):  # #7335
                return False
            rec = recording_of(tid)
            return rec is None or (rec not in recent_recs and rec not in played_before)

        def why_empty(lane: dict, recent: set[int]) -> str:
            ids = lane.get("track_ids") or []
            if not ids:
                return _why_no_tracks(lane.get("dropped"))
            # #4051: one reason per track, first match wins. The old catch-all called
            # every queued / work-blocked track "a copy of a recently picked recording".
            counts = {"banned": 0, "recent": 0, "queued": 0, "before": 0, "work": 0, "copy": 0, "other": 0}
            for t in ids:
                rec = recording_of(t)
                key = ("banned" if t in banned else "recent" if t in recent
                       else "queued" if t in skip_ids else "before" if rec in played_before
                       else "work" if not work_ok(t) else "copy" if rec in skip_recordings else "other")
                counts[key] += 1
            parts = [f"{counts['recent']} picked within the last {window} picks",
                     f"{counts['queued']} already queued or picked this session",
                     f"{counts['before']} played recently outside this pool",
                     f"{counts['work']} another version of a song already queued or in cooldown",
                     f"{counts['banned']} banned",
                     f"{counts['copy']} a copy of a recording already queued"]
            if counts["other"]:
                parts.append(f"{counts['other']} other")
            return f"all {len(ids)} track(s) unavailable: " + ", ".join(parts)

        def others_fresh(name: str) -> bool:
            return any(eligible(t) for other, data in self.state["lanes"].items()
                       if other != name and not data.get("paused") for t in data.get("track_ids", []))

        # Round-robin + starvation; filtered lazily so a pick blocks its own
        # recording's other copies within the same top-up. Within a lane the
        # pick is random (#7108: shuffled, not stored album order); a lane that
        # has played everything recycles outside the no-repeat window while
        # other lanes still have fresh tracks, so a small bucket keeps its turn
        # instead of starving out. A pool with nothing fresh anywhere is done.
        held: list[dict] = []  # lanes blocked only by the window: retried after the next pick
        while len(picks) < to_pick:
            selected_lane = self._select_next_lane()
            if not selected_lane:
                break
            lane = self.state["lanes"][selected_lane]
            ids = lane.get("track_ids", [])
            fresh = [t for t in ids if eligible(t)]
            if fresh:
                pick = random.choice(fresh)
            else:
                recent = recent_window()
                recent_recs = {r for r in map(recording_of, recent) if r is not None}
                again = [t for t in ids if recyclable(t, recent, recent_recs)]
                if not again or not others_fresh(selected_lane):
                    lane["exhausted"] = True
                    if not again:
                        held.append(lane)
                    lane["why_empty"] = why_empty(lane, recent) if not again else (
                        f"all {len(ids)} track(s) already played or queued, and no other lane "
                        "has fresh tracks left to recycle alongside")
                    continue
                pick = random.choice(again)
            lane["why_empty"] = None

            picks.append(pick)
            for blocked in held:  # #7108: the window moved on, so try them again
                blocked["exhausted"] = False
            held.clear()
            skip_ids.add(pick)
            if pick in work_of:  # #7335: this song is now spoken for in this top-up
                blocked_works.add(work_of[pick])  # #7335
            rec = recording_of(pick)
            if rec is not None:
                skip_recordings.add(rec)

            lane["played_count"] = lane.get("played_count", 0) + 1
            lane["last_pick_index"] = self.state.get("round_robin_index", 0)  # #7108
            lane["last_played_at"] = time.time()
            self.state["round_robin_index"] = self.state.get("round_robin_index", 0) + 1

        # #7108: a lane blocked only for now (window, queue) is tried again at
        # the next top-up; only a lane with no tracks at all stays exhausted.
        for lane_data in self.state.get("lanes", {}).values():
            if lane_data.get("track_ids"):
                lane_data["exhausted"] = False
        lane_details = [
            {
                "label": lane_name,
                "remaining": len(lane_data.get("track_ids", [])),
                "played_count": lane_data.get("played_count", 0),
                "exhausted": lane_data.get("exhausted", False),
                "why_empty": lane_data.get("why_empty"),  # #7108
            }
            for lane_name, lane_data in self.state.get("lanes", {}).items()
        ]

        self.state["last_topup_current_track_id"] = current_track_id
        if not picks:
            self._persist()
            return {"picks": [], "reason": "no eligible candidates", "pending_cues": pending_cues,
                    "cue_picks": [], "per_lane_details": lane_details}

        # Record picks as played
        self.state.setdefault("played_this_session", []).extend(picks)
        self.state["last_topup_at"] = time.time()
        self._persist()

        return {
            "picks": picks,
            "cue_picks": list(cue_picks),  # #3644: these go in next, not at the end
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

    def replan(
        self,
        current_track_id: int,
        upcoming_track_ids: list[int],
        played_track_ids: list[int],
        db: Optional[Any] = None,
    ) -> list[int]:
        """Re-pick everything after the current song under the pool's current
        lanes (#3644). With a deep queue a lane pause or a spec edit would
        otherwise only show up hours later. The queued-but-unplayed picks are
        forgotten so they can be picked again; already-played ones stay out."""
        if not self.is_active() or current_track_id is None or current_track_id < 0:
            return []
        dropped = set(upcoming_track_ids)
        self.state["played_this_session"] = [
            t for t in self.state.get("played_this_session", []) if t not in dropped]
        for lane in self.state.get("lanes", {}).values():
            if not lane.get("paused"):
                lane["exhausted"] = False  # forgotten picks make it eligible again
        result = self.top_up(
            current_track_id=current_track_id,
            upcoming_track_ids=[current_track_id],
            current_played_track_ids=played_track_ids,
            cues=[],
            db=db,
        )
        return list(result.get("picks") or [])

    def forget_picks(self, track_ids: list[int]) -> int:
        """#4051: picks that never reached the phone (trim refused / nothing sent)
        leave the session history, so they neither count nor block the next top-up."""
        drop = {int(t) for t in track_ids}
        before = self.state.get("played_this_session", [])
        self.state["played_this_session"] = [t for t in before if t not in drop]
        self._persist()
        return len(before) - len(self.state["played_this_session"])

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
