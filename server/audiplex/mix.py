"""Plan shuffled multi-source mixes — Todd's standing order (#2842).

On every add to a running mix: trim what already played, dedupe across
folders, reshuffle, and never interrupt the current song. The current track is
untouchable: this plans only what goes AFTER it, and a candidate that is the
same recording as the current track is treated as played, so the next song can
never be the one that just played in another folder's copy.

Pure on purpose — no DB, no I/O — so the invariants are unit-testable. The
caller supplies recording keys (identity.build_identity_map), which is what
makes "the same file in two folders" one entry.
"""

from dataclasses import dataclass
import random


@dataclass(frozen=True)
class MixPlan:
    """Describe the upcoming queue and why candidates were removed."""

    upcoming: list[int]
    kept_from_queue: int
    added: int
    trimmed_played: list[int]
    trimmed_duplicates: list[int]


def plan_mix(
    current_id: int | None,
    played_ids: list[int],
    upcoming_ids: list[int],
    new_ids: list[int],
    recording_key: dict[int, str],
    *,
    shuffle: bool = True,
    seed: int | None = None,
) -> MixPlan:
    """Build the queue after the current track without interrupting playback."""

    def key(track_id: int) -> str:
        return recording_key.get(track_id, f"id:{track_id}")

    played_keys = {key(track_id) for track_id in played_ids}
    if current_id is not None:
        played_keys.add(key(current_id))

    pool = [
        *((track_id, False) for track_id in upcoming_ids),
        *((track_id, True) for track_id in new_ids),
    ]

    kept: list[tuple[int, bool]] = []
    kept_keys: set[str] = set()
    trimmed_played: list[int] = []
    trimmed_played_ids: set[int] = set()
    trimmed_duplicates: list[int] = []

    for track_id, is_new in pool:
        track_key = key(track_id)
        if track_key in played_keys:
            if track_id not in trimmed_played_ids:
                trimmed_played.append(track_id)
                trimmed_played_ids.add(track_id)
            continue
        if track_key in kept_keys:
            trimmed_duplicates.append(track_id)
            continue

        kept.append((track_id, is_new))
        kept_keys.add(track_key)

    if shuffle:
        random.Random(seed).shuffle(kept)

    return MixPlan(
        upcoming=[track_id for track_id, _ in kept],
        kept_from_queue=sum(not is_new for _, is_new in kept),
        added=sum(is_new for _, is_new in kept),
        trimmed_played=trimmed_played,
        trimmed_duplicates=trimmed_duplicates,
    )


def plan_summary(plan: MixPlan) -> str:
    """Summarize the mix update for concise user-facing status text."""

    duplicate_count = len(plan.trimmed_duplicates)
    duplicate_label = "duplicate" if duplicate_count == 1 else "duplicates"
    return (
        f"{len(plan.upcoming)} upcoming "
        f"({plan.kept_from_queue} kept, {plan.added} new); "
        f"trimmed {len(plan.trimmed_played)} already played, "
        f"{duplicate_count} {duplicate_label}"
    )
