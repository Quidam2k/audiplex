"""#7116: speed-aware ride volume — the reference the companion's ride auto-volume ports.

Wind noise grows with speed and the boom mic can't measure it, so GPS speed is
the proxy. Measured on the 2026-10-06 ride (scripts/volume_analysis.py): Todd
sat a median 15/25 steps moving and 13/25 stopped, turned it DOWN within 30 s
of stopping (5 down, 1 up) and UP more than down when fast (>=6 m/s: 24 up,
8 down). So: stopped = -2 steps, fast = +1 step, cruise = his own level.

Hysteresis keeps it from pumping at every light: a state changes only after
the speed has stayed past the far threshold for the dwell time. Feed one
sample per GPS fix; the result is an offset in device volume STEPS added to
the level Todd set (the companion clamps to its own min/max).
"""

from __future__ import annotations

from dataclasses import dataclass, field

STOPPED, CRUISE, FAST = "stopped", "cruise", "fast"
OFFSET_STEPS = {STOPPED: -2, CRUISE: 0, FAST: 1}

# (enter when speed is past this, for this many seconds)
ENTER = {
    STOPPED: (lambda v: v < 1.0, 8.0),
    FAST: (lambda v: v >= 6.0, 10.0),
    CRUISE: (lambda v: 2.5 <= v < 5.0, 4.0),
}


@dataclass
class SpeedGain:
    state: str = CRUISE
    _pending: str | None = None
    _since: float | None = None
    history: list = field(default_factory=list)

    def update(self, t: float, speed_mps: float | None) -> int:
        """Advance with one GPS fix at time t (seconds); return the offset in steps."""
        if speed_mps is None:  # no fix: hold
            return OFFSET_STEPS[self.state]
        candidate = next((s for s, (test, _) in ENTER.items() if s != self.state and test(speed_mps)), None)
        if candidate is None:
            self._pending = self._since = None
        elif candidate != self._pending:
            self._pending, self._since = candidate, t
        elif t - self._since >= ENTER[candidate][1]:
            self.state, self._pending, self._since = candidate, None, None
            self.history.append((t, candidate))
        return OFFSET_STEPS[self.state]
