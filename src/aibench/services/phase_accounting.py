"""Shared accounting for work whose application dispatch lost its result record."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from aibench.core.models import WorkItem


def recovered_uncommitted_warmup_dispatches(
    events: Sequence[Mapping[str, object]], work_items: Sequence[WorkItem]
) -> int:
    """Count recovered warmup dispatches that had no committed attempt.

    Recovery events retain the task key and settlement reason. That lets reports classify
    their unknown-cost calls even when no ExecutionResult exists to carry the phase marker.
    """
    warmup_keys = {
        item.task_key for item in work_items if item.kind == "execution" and item.warmup
    }
    count = 0
    for event in events:
        if event.get("event_type") != "recovered":
            continue
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            continue
        notes = payload.get("items")
        if not isinstance(notes, Sequence) or isinstance(notes, str | bytes):
            continue
        for note in notes:
            if not isinstance(note, str):
                continue
            task_key, separator, detail = note.rpartition(": ")
            if (
                separator
                and task_key in warmup_keys
                and detail.startswith(("re-dispatch", "unknown_effect"))
            ):
                count += 1
    return count
