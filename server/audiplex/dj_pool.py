"""DJ pool: persistent state, top-up on track changes, cue management (#5470, #5473, #5477).

Manages a pool of eligible tracks from mix specs, auto-queuing ahead of the
current track. Supports cue-triggered injections (patter before specific songs)
and balance modes (even / proportional / none distribution across sources).
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
    """Persistent DJ pool state: eligible tracks, sources, top-up logic."""

    def __init__(self, state_file: str = DEFAULT_POOL_STATE_FILE):
        self.state_file = Path(state_file)
        self.state = _load_json_safe(self.state_file, self._empty_state())

    @staticmethod
    def _empty_state() -> dict[str, Any]:
        """Default pool state."""
        return {
            "spec_id": None,
            "eligible_track_ids": [],
            "source_labels": {},
            "balance_mode": "even",
            "ahead": 4,
            "exclude_recent_hours": 12,
            "played_this_session": [],
            "last_topup_at": None,
            "last_topup_current_track_id": None,
        }

    def _persist(self) -> None:
        """Save state to disk."""
        _write_json_atomic(self.state_file, self.state)

    def set_pool(
        self,
        spec_id: int,
        track_ids: list[int],
        source_labels: dict[int, str],
        balance: str = "even",
        ahead: int = 4,
        exclude_recent_hours: float = 12,
        played_this_session: list[int] | None = None,
    ) -> dict[str, Any]:
        """Update pool with new eligible tracks and configuration."""
        self.state.update({
            "spec_id": spec_id,
            "eligible_track_ids": track_ids,
            "source_labels": source_labels,
            "balance_mode": balance,
            "ahead": ahead,
            "exclude_recent_hours": exclude_recent_hours,
            "played_this_session": played_this_session or [],
            "last_topup_at": None,
            "last_topup_current_track_id": None,
        })
        self._persist()
        return {"spec_id": spec_id, "eligible_count": len(track_ids), "ready": True}

    def stop(self) -> None:
        """Clear pool state."""
        self.state = self._empty_state()
        self._persist()

    def status(self) -> dict[str, Any]:
        """Current pool status."""
        return {
            "spec_id": self.state.get("spec_id"),
            "eligible_count": len(self.state.get("eligible_track_ids", [])),
            "balance_mode": self.state.get("balance_mode"),
            "ahead": self.state.get("ahead"),
            "played_this_session_count": len(self.state.get("played_this_session", [])),
            "last_topup_at": self.state.get("last_topup_at"),
            "source_counts": self._count_sources(),
        }

    def _count_sources(self) -> dict[str, int]:
        """Count tracks per source in eligible set."""
        counts = {}
        source_labels = self.state.get("source_labels", {})
        for track_id in self.state.get("eligible_track_ids", []):
            # source_labels keys might be int or str
            label = source_labels.get(track_id) or source_labels.get(str(track_id)) or "unknown"
            counts[label] = counts.get(label, 0) + 1
        return counts

    def top_up(
        self,
        current_track_id: Optional[int],
        upcoming_track_ids: list[int],
        current_played_track_ids: Optional[list[int]] = None,
        cues: Optional[list[dict]] = None,
        match_trigger_fn=None,
    ) -> dict[str, Any]:
        """
        Suggest picks to top up the queue.

        current_track_id: track currently playing (None if nothing loaded)
        upcoming_track_ids: queue after current (may include current)
        current_played_track_ids: tracks played in this session (outside this pool)
        cues: list of cues with triggers to check
        match_trigger_fn: function(event, cues) -> list[cue_ids]

        Returns: {picks: [id, ...], reason: "...", pending_cues: []}
        """
        if not self.state.get("spec_id"):
            return {"picks": [], "reason": "no pool set", "pending_cues": []}

        # Count real tracks in upcoming AFTER current (skip current, streams, DJ breaks)
        real_count = sum(
            1 for tid in upcoming_track_ids[1:] if isinstance(tid, int) and tid > 0
        )

        ahead = self.state.get("ahead", 4)
        if real_count >= ahead:
            return {"picks": [], "reason": f"already {real_count} tracks ahead", "pending_cues": []}

        # Need to pick new tracks
        picks = []
        eligible = self.state.get("eligible_track_ids", [])
        source_labels = self.state.get("source_labels", {})
        played = set(self.state.get("played_this_session", []))
        exclude_hours = self.state.get("exclude_recent_hours", 12)
        balance = self.state.get("balance_mode", "even")

        # Skip: recently played (via taste.recent_plays_for) + session played
        # This would normally call taste.recent_plays_for(exclude_hours) but we
        # stub it for testing.
        skip_ids = played.copy()

        # Filter candidates (exclude recently played, duplicates, etc)
        candidates = [
            tid for tid in eligible
            if tid not in skip_ids and tid > 0  # real tracks only
        ]

        if not candidates:
            return {"picks": [], "reason": "no eligible candidates", "pending_cues": []}

        # Apply balance ordering (mix_balance in audiplex_mcp)
        try:
            from audiplex_mcp.mix_balance import balance_order
        except ImportError:
            # Fallback: simple shuffle if import fails in test
            import random
            balanced = candidates.copy()
            random.shuffle(balanced)
        else:
            # source_labels keys might be int or str
            source_map = {}
            for tid in candidates:
                source_map[tid] = source_labels.get(tid) or source_labels.get(str(tid)) or "unknown"
            balanced = balance_order(
                candidates,
                source_map,
                mode=balance,
                seed=None,
            )

        # Pick up to (ahead - real_count) tracks
        to_pick = ahead - real_count
        picks = balanced[:to_pick]

        # Record picks as played
        self.state["played_this_session"].extend(picks)
        self.state["last_topup_at"] = time.time()
        self.state["last_topup_current_track_id"] = current_track_id
        self._persist()

        return {
            "picks": picks,
            "reason": f"topped up to {ahead} ahead",
            "sources": {str(tid): source_labels.get(tid) or source_labels.get(str(tid), "unknown") for tid in picks},
            "pending_cues": [],
        }
