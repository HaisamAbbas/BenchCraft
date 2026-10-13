"""Arrival-time calculations for bounded open-loop benchmark scheduling."""

from __future__ import annotations

import math

from aibench.core.plans import LoadProfile


def arrival_offset_seconds(profile: LoadProfile, ordinal: int) -> float | None:
    """Return the target offset for a first-attempt arrival, or None for closed loop.

    Ordinal zero is offered immediately. A ramp linearly interpolates rate from its
    starting to final rate for the configured duration, then holds the final rate.
    """
    if ordinal < 0:
        raise ValueError("arrival ordinal cannot be negative")
    if profile.mode == "closed_loop":
        return None
    if profile.mode == "open_loop":
        return ordinal / profile.requests_per_second

    start = profile.start_requests_per_second
    end = profile.end_requests_per_second
    duration = profile.ramp_duration_seconds
    ramp_arrivals = (start + end) * duration / 2
    if ordinal <= ramp_arrivals:
        # Stable positive root for the integral of a linearly increasing rate.
        discriminant = start * start + 2 * (end - start) * ordinal / duration
        return 2 * ordinal / (start + math.sqrt(discriminant))
    return duration + (ordinal - ramp_arrivals) / end
