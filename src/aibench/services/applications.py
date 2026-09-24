"""What an application's runner can and cannot show (§7 "observability-gap report", 15-T3),
shared by `aibench app describe`, the chat's `/app` and the assistant's
`describe_application` tool. Pure: nothing is started, invoked or reset.
"""

from __future__ import annotations

from typing import Any

from aibench.runners import LoadedApplication, create_runner, reset_hook
from aibench.security.policy import ExecutionPolicy, policy_matches
from aibench.services.runs import reset_mode

# What a missing capability means for a benchmark, in plain words.
_MISSING: dict[str, str] = {
    "retrieved_context": "groundedness and faithfulness checks need the passages the "
    "application actually retrieved; without them they are gaps, not scores",
    "tool_events": "tool checks need the tool calls the application made; without them "
    "they are gaps",
    "usage": "token usage is unknown",
    "cost": "cost is unknown, never shown as $0",
    "world_state": "final-state checks need the test world's state after each case; "
    "without it they are gaps",
}


def describe_application(
    loaded: LoadedApplication, policy: ExecutionPolicy | None = None
) -> dict[str, Any]:
    spec = loaded.spec
    runner = create_runner(loaded)  # constructing a runner starts nothing
    description = runner.describe()
    hook = reset_hook(spec)
    mode = reset_mode(spec, world_selected=False)
    if mode == "per_episode":
        reset = "before each episode (cases sharing a group_id run in order and share state)"
    elif mode == "per_case":
        reset = f"before every case, through its {hook}"
    elif spec.reset_policy.value == "shared":
        reset = "never: the application declares shared state between cases"
    else:
        reset = "no reset hook: state the application keeps outside a fresh process is not reset"
    worlds = [
        {
            "world_id": world_id,
            "description": world.description,
            "approved": policy is not None
            and policy_matches(f"{spec.application_id}:{world_id}", policy.allowed_test_worlds),
            "loadable": hook is not None,
        }
        for world_id, world in sorted(spec.test_worlds.items())
    ]
    missing = [
        {"capability": capability, "consequence": _MISSING[capability]}
        for capability, state in description.observable.items()
        if state == "unknown" and capability in _MISSING
    ]
    return {
        "application_id": spec.application_id,
        "kind": description.kind,
        "target": description.target,
        "effects": description.effects,
        "isolation": description.isolation,
        "reset": {
            "policy": spec.reset_policy.value,
            "hook": hook,
            "mode": mode,
            "summary": reset,
        },
        "observable": description.observable,
        "missing_evidence": missing,
        "test_worlds": worlds,
        "limitations": list(description.limitations),
    }
