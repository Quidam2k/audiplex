"""Balance DJ mix ordering across sources.

Ticket #5463: multi-source ``dj_mix`` calls queued whole sources in blocks
(992 Ren Faire tracks, then 277 YouTube tracks); Todd wants a broad mix.
"""

import random  # #5463


_VALID_MODES = ("even", "proportional", "none")


def balance_order(
    ids: list[int],
    source_of: dict[int, str],
    mode: str = "even",
    seed: int | None = None,
    unlabeled: str = "already queued",
) -> list[int]:
    """Return a source-balanced permutation of track IDs."""
    if mode not in _VALID_MODES:
        valid = ", ".join(f'"{name}"' for name in _VALID_MODES)
        raise ValueError(f"unknown mode {mode!r}; valid modes are {valid}")
    if not ids:
        return []

    rng = random.Random(seed)
    if mode == "none":
        result = ids.copy()
        rng.shuffle(result)
        return result

    groups: dict[str, list[int]] = {}
    for track_id in ids:
        label = source_of.get(track_id, unlabeled)
        groups.setdefault(label, []).append(track_id)

    for group in groups.values():
        rng.shuffle(group)

    if mode == "proportional":
        positioned: list[tuple[float, int]] = []
        for group in groups.values():
            size = len(group)
            for index, track_id in enumerate(group):
                key = (index + 0.5) / size + rng.random() * 1e-9
                positioned.append((key, track_id))
        positioned.sort(key=lambda item: item[0])
        return [track_id for _, track_id in positioned]

    positions = {label: 0 for label in groups}
    active = list(groups)
    result: list[int] = []
    while active:
        round_order = active.copy()
        rng.shuffle(round_order)
        for label in round_order:
            index = positions[label]
            result.append(groups[label][index])
            positions[label] = index + 1
        active = [
            label
            for label in active
            if positions[label] < len(groups[label])
        ]
    return result


def source_counts(
    ids: list[int],
    source_of: dict[int, str],
    unlabeled: str = "already queued",
) -> dict[str, int]:
    """Count tracks by source in first-appearance order."""
    counts: dict[str, int] = {}
    for track_id in ids:
        label = source_of.get(track_id, unlabeled)
        counts[label] = counts.get(label, 0) + 1
    return counts


def describe_head(
    ids: list[int],
    source_of: dict[int, str],
    n: int = 10,
    unlabeled: str = "already queued",
) -> str:
    """Summarize source counts among the next tracks."""
    counts = source_counts(ids[:n], source_of, unlabeled)
    n = min(n, len(ids))  # #5463
    details = ", ".join(
        f"{count} {label}" for label, count in counts.items()
    )
    return f"next {n}: {details}"
