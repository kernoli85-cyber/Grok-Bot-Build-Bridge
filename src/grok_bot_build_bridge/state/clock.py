"""Injectable clock/random for deterministic retry tests."""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field


@dataclass
class Clock:
    """Monotonic-ish wall clock for scheduling; tests freeze/advance `now`."""

    _now: float | None = None

    def time(self) -> float:
        if self._now is None:
            return time.time()
        return self._now

    def set(self, value: float) -> None:
        self._now = value

    def advance(self, seconds: float) -> float:
        if self._now is None:
            self._now = time.time()
        self._now += seconds
        return self._now


@dataclass
class Jitter:
    """Deterministic jitter when seeded; real random otherwise."""

    seed: int | None = None
    _rng: random.Random = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)

    def uniform(self, lo: float, hi: float) -> float:
        return self._rng.uniform(lo, hi)
