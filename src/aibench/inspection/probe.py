"""Controlled probes (§8 "Dynamic probes are engine requests subject to existing policy",
16-T1): invoke a few dataset cases through the ordinary runner, only when the policy
permits the application, and record them like any developer smoke run. What the recorded
responses contain then counts as *observed* evidence in the profile.

A probe is never free-form code or shell access: it is the same invocation path a run
uses, with the same policy, trust and effect rules, and no retries or evaluation.
"""

from __future__ import annotations

from pathlib import Path

from aibench.core.errors import AibenchError, PolicyError
from aibench.core.models import EffectLevel
from aibench.datasets.ingest import ingest_dataset
from aibench.runners import LoadedApplication, create_runner
from aibench.security.policy import ExecutionPolicy, application_denials
from aibench.services.execution import SmokeReport, run_developer_smoke
from aibench.storage.artifacts import ArtifactStore
from aibench.storage.repositories import Storage


class ProbeRefused(PolicyError):
    def __init__(self, denials: list[str]) -> None:
        super().__init__("; ".join(denials))
        self.denials = denials


def probe_denials(loaded: LoadedApplication, policy: ExecutionPolicy) -> list[str]:
    """Why the policy does not permit probing this application. An application with
    declared effects is never probed: a probe is not a benchmark and has no effect
    handling."""
    denials = application_denials(policy, loaded.spec)
    if loaded.spec.effects is not EffectLevel.NONE:
        denials.append(
            f"application declares {loaded.spec.effects.value} effects; probes only run "
            "effect-free applications"
        )
    return denials


async def probe_application(
    loaded: LoadedApplication,
    dataset: Path,
    *,
    policy: ExecutionPolicy,
    storage: Storage,
    artifacts: ArtifactStore,
    limit: int,
) -> SmokeReport:
    """Invoke the first `limit` cases once each. Refused, with nothing invoked, unless the
    policy permits the application (`probe_denials`)."""
    denials = probe_denials(loaded, policy)
    if denials:
        raise ProbeRefused(denials)
    report = ingest_dataset(dataset)
    if not report.is_valid or report.manifest is None:
        raise AibenchError("dataset is invalid; run `aibench dataset validate` for details")
    runner = create_runner(loaded, trusted_local=policy.allow_trusted_local)
    async with runner:
        return await run_developer_smoke(
            runner,
            loaded.spec,
            report.manifest,
            report.cases[:limit],
            storage=storage,
            artifacts=artifacts,
        )
