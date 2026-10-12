"""#4056: the pool's picks lean on song memory (#3984 song_context.after).

A lane is still chosen by strict rotation; inside it, a candidate that stored
evidence says plays well after the previous song weighs BOOST times more. It
is a tilt, never a filter or an order, so lanes and variety are unchanged.
Fail-open: any error, or the per-top-up time budget running out, means the
rest of that top-up picks exactly as before. Rollback: dj_context_picks=False.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional

BOOST = 2.0
BUDGET_S = 1.5  # same ceiling #3984 gives a queue call
AFTER_N = 500  # after() ranks evidence-reached tracks; keep enough to meet the lane

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="dj_context")


def _default_after(track_id: int, n: int) -> list[dict[str, Any]]:
    from audiplex_mcp import song_context

    return song_context.after(track_id, n)


class ContextTilt:
    """One per top-up: caches after() per seed and stops for good once over budget."""

    def __init__(self, enabled: bool, budget_s: float = BUDGET_S,
                 after_fn: Optional[Callable[[int, int], list[dict]]] = None):
        self.enabled = enabled
        self.deadline = time.monotonic() + budget_s
        self.after_fn = after_fn or _default_after
        self.cache: dict[int, dict[int, dict]] = {}
        self.gave_up: Optional[str] = None

    def _links(self, seed: int) -> dict[int, dict]:
        if seed not in self.cache:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("song memory time budget used up")
            rows = _executor.submit(self.after_fn, seed, AFTER_N).result(timeout=remaining)
            self.cache[seed] = {r["track_id"]: r for r in rows or [] if isinstance(r, dict) and "track_id" in r}
        return self.cache[seed]

    def weights(self, seed: Optional[int], candidates: list[int],
                base: dict[int, float]) -> tuple[dict[int, float], dict[int, dict]]:
        """Learned weights with linked candidates boosted, and the links that did it."""
        if not self.enabled or self.gave_up or not isinstance(seed, int) or seed <= 0:
            return base, {}
        try:
            links = self._links(seed)
        except Exception as e:  # fail-open: this top-up picks as before
            self.gave_up = f"{type(e).__name__}: {e}"
            print(f"[dj_pool] song memory skipped for this top-up: {self.gave_up}", flush=True)
            return base, {}
        hit = {t: links[t] for t in candidates if t in links}
        if not hit:
            return base, {}
        tilted = dict(base)
        for t in hit:
            tilted[t] = base.get(t, 1.0) * BOOST
        return tilted, hit


def reason_text(seed: int, link: dict) -> str:
    """'plays well after #12: DJ note' from after()'s first reason."""
    reasons = link.get("reasons") or []
    first = reasons[0] if reasons else {}
    text = first.get("text") or first.get("kind") or "linked"
    return f"plays well after #{seed}: {text}"
