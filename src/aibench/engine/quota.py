"""Provider-aware quotas and backpressure (§15, 16-T4).

A quota is shared by all the work it applies to: the application, or evaluators matching a
glob (e.g. every judge that calls the same provider). It bounds work in flight and the
start rate (a token bucket: `requests_per_second`, with `burst` tokens), and it backs off:
when the provider answers "slow down" (HTTP 429 or 503), no new work under that quota
starts until its `Retry-After`, or `backoff_seconds` when none is given. The engine asks a
gate before it creates a task, so throttled work waits as a queue entry, never as a
coroutine.
"""

from __future__ import annotations

import fnmatch
import math
import time
from collections.abc import Callable

from aibench.core.plans import Quota


class QuotaGate:
    def __init__(self, quota: Quota, clock: Callable[[], float] = time.monotonic) -> None:
        self.quota = quota
        self.clock = clock
        self.tokens = float(quota.burst)
        self.updated = clock()
        self.in_flight = 0
        self.cooldown_until = 0.0
        self.started = 0
        self.backpressure_events = 0
        self.max_in_flight_seen = 0

    def applies_to(self, kind: str, evaluator_id: str | None = None) -> bool:
        target = self.quota.applies_to
        if target == "application":
            return kind == "execution"
        return (
            kind == "evaluation"
            and evaluator_id is not None
            and fnmatch.fnmatchcase(evaluator_id, target.removeprefix("evaluator:"))
        )

    def _refill(self, now: float) -> None:
        rate = self.quota.requests_per_second
        if rate is not None:
            self.tokens = min(float(self.quota.burst), self.tokens + (now - self.updated) * rate)
        self.updated = now

    def wait_seconds(self) -> float:
        """0 when work may start now; otherwise how long until it might (inf: only when
        work in flight finishes)."""
        now = self.clock()
        self._refill(now)
        if now < self.cooldown_until:
            return self.cooldown_until - now
        cap = self.quota.max_in_flight
        if cap is not None and self.in_flight >= cap:
            return math.inf
        rate = self.quota.requests_per_second
        if rate is not None and self.tokens < 1.0:
            return (1.0 - self.tokens) / rate
        return 0.0

    def start(self) -> None:
        self._refill(self.clock())
        if self.quota.requests_per_second is not None:
            self.tokens -= 1.0
        self.in_flight += 1
        self.started += 1
        self.max_in_flight_seen = max(self.max_in_flight_seen, self.in_flight)

    def finish(self) -> None:
        self.in_flight = max(0, self.in_flight - 1)

    def backpressure(self, retry_after: float | None) -> float:
        """The provider asked to slow down: pause new work under this quota. Returns the
        pause in seconds: its Retry-After, capped by `max_backpressure_seconds`, or the
        quota's backoff when it gave none."""
        pause = retry_after if retry_after is not None else self.quota.backoff_seconds
        pause = min(max(pause, 0.0), self.quota.max_backpressure_seconds)
        self.cooldown_until = max(self.cooldown_until, self.clock() + pause)
        self.backpressure_events += 1
        return pause

    def summary(self) -> dict[str, object]:
        return {
            "quota": self.quota.name,
            "applies_to": self.quota.applies_to,
            "started": self.started,
            "max_in_flight_seen": self.max_in_flight_seen,
            "backpressure_events": self.backpressure_events,
        }
