"""Shared accounting for work whose application dispatch lost its result record."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from aibench.core.models import WorkItem


def recovered_uncommitted_execution_items(
    events: Sequence[Mapping[str, object]], work_items: Sequence[WorkItem]
) -> list[WorkItem]:
    """Return work items with an uncommitted application dispatch, preserving repeats.

    Recovery events retain the task key and settlement reason. That lets reports classify
    their unknown-cost calls even when no ExecutionResult exists to carry the phase marker.
    """
    work_by_key = {
        item.task_key: item for item in work_items if item.kind == "execution"
    }
    recovered: list[WorkItem] = []
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
                and task_key in work_by_key
                and detail.startswith(("re-dispatch", "unknown_effect"))
            ):
                recovered.append(work_by_key[task_key])
    return recovered


def recovered_uncommitted_warmup_dispatches(
    events: Sequence[Mapping[str, object]], work_items: Sequence[WorkItem]
) -> int:
    """Count recovered warmup dispatches that had no committed attempt."""
    return sum(item.warmup for item in recovered_uncommitted_execution_items(events, work_items))
