"""User-reviewed candidate data operations (18-T1/2)."""

# Typer intentionally builds CLI parameters from `typer.Option` annotations.
# ruff: noqa: B008

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import typer
from pydantic import ValidationError as PydanticValidationError
from rich.markup import escape

from aibench.cli.errors import error_exit
from aibench.cli.output import Console
from aibench.config.resolve import load_mapping_file
from aibench.core.errors import AibenchError
from aibench.datasets.candidates import (
    GENERATION_OUTPUT_TOKENS,
    CandidateGenerationError,
    generate_candidate_pool,
    source_text_for_span,
)
from aibench.engine.compile import load_policy
from aibench.planning.openai_provider import (
    OpenAICompatibleConfig,
    OpenAICompatibleProvider,
    provider_denials,
)
from aibench.services.candidates import (
    path_denials,
    promote_candidates,
    record_candidate_executable_check,
    record_candidate_review,
)
from aibench.storage.db import Database, Workspace
from aibench.storage.repositories import Storage

app = typer.Typer(help="Generate, review, verify, and promote dataset candidates.")
console = Console()
err_console = Console(stderr=True)


def _fail(message: str, code: int = 2) -> typer.Exit:
    return error_exit(
        message, exit_code=code, json_output=False, console=console, err_console=err_console
    )


def _open_storage(root: Path | None) -> tuple[Workspace, Storage]:
    workspace = Workspace.at(root or Path.cwd())
    return workspace, Storage(Database.open_workspace(workspace))


@app.command("generate")
def generate(
    sources: list[Path] = typer.Argument(..., help="UTF-8 .txt/.md source documents."),
    provider_config: Path = typer.Option(
        ..., "--provider-config", help="OpenAI-compatible provider JSON."
    ),
    split: str = typer.Option(
        ..., "--split", help="Must be development; held-out data cannot be read."
    ),
    policy: Path | None = typer.Option(
        None, "--policy", help="Provider egress, secret, and data-root policy."
    ),
    workspace: Path | None = typer.Option(
        None, "--workspace", help="Project root containing .aibench/."
    ),
    max_candidates: int = typer.Option(20, "--max-candidates", min=1, max=50),
    json_output: bool = typer.Option(False, "--json", help="Print a machine-readable summary."),
) -> None:
    """Generate a bounded pool in one provider call; generated records remain candidates."""
    provider = None
    try:
        if split != "development":
            raise CandidateGenerationError(
                "candidate generation is restricted to --split development; held-out data is never sent"
            )
        loaded_policy = load_policy(policy)
        if denials := path_denials(loaded_policy, tuple(sources)):
            raise CandidateGenerationError("; ".join(denials))
        config = OpenAICompatibleConfig.model_validate(load_mapping_file(provider_config))
        # Candidate jobs have their own output allowance, whatever a shared planner config
        # says: a smaller one cannot hold the cases of a model that thinks first, a larger one
        # is not needed.
        if config.max_output_tokens != GENERATION_OUTPUT_TOKENS:
            config = config.model_copy(update={"max_output_tokens": GENERATION_OUTPUT_TOKENS})
        if denials := provider_denials(config, loaded_policy):
            raise CandidateGenerationError("; ".join(denials))
        provider = OpenAICompatibleProvider(config)
        pool_id = "pool-" + uuid4().hex
        manifest, candidates = generate_candidate_pool(
            tuple(sources),
            provider,
            pool_id=pool_id,
            source_split=split,
            max_candidates=max_candidates,
        )
        _workspace, storage = _open_storage(workspace)
        try:
            storage.commit_candidate_pool(manifest, candidates)
        finally:
            storage.db.close()
        payload = {
            "pool_id": pool_id,
            "split_id": "development",
            "candidate_count": len(candidates),
            "candidate_ids": [candidate.candidate_id for candidate in candidates],
            "duplicate_sources": [
                {"source_ref": item.source_ref, "duplicate_of": item.duplicate_of}
                for item in manifest.sources
                if item.duplicate_of
            ],
            "status": "candidate",
        }
        if json_output:
            console.print_json(data=payload)
        else:
            console.print(
                f"candidate pool {escape(pool_id)}: {len(candidates)} development candidate(s); "
                "all references are synthetic_unverified"
            )
            for candidate in candidates:
                console.print(f"  {escape(candidate.candidate_id)}")
            if payload["duplicate_sources"]:
                console.print(f"Exact duplicate source documents: {payload['duplicate_sources']}")
    except (AibenchError, OSError, PydanticValidationError, ValueError) as exc:
        raise _fail(str(exc)) from exc
    finally:
        if provider is not None:
            provider.close()


@app.command("list")
def list_candidates(
    pool_id: str = typer.Argument(..., help="Candidate pool identity."),
    workspace: Path | None = typer.Option(
        None, "--workspace", help="Project root containing .aibench/."
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """List candidate states without merging them into benchmark datasets."""
    try:
        _ws, storage = _open_storage(workspace)
        try:
            manifest = storage.get_candidate_pool(pool_id)
            if manifest is None:
                raise AibenchError(f"no candidate pool with pool_id={pool_id!r}")
            candidates = storage.list_candidates(pool_id)
        finally:
            storage.db.close()
        payload = {
            "pool": manifest.model_dump(mode="json"),
            "candidates": [
                {
                    "candidate_id": item.candidate_id,
                    "status": item.status.value,
                    "reference_status": item.case.reference.status.value
                    if item.case.reference
                    else None,
                    "input": item.case.input,
                }
                for item in candidates
            ],
        }
        if json_output:
            console.print_json(data=payload)
        else:
            console.print(f"pool {escape(pool_id)} ({escape(manifest.split_id)})")
            for item in candidates:
                console.print(
                    f"  {escape(item.candidate_id)}  {item.status.value}  "
                    f"{escape(str(item.case.input)[:100])}"
                )
    except (AibenchError, OSError) as exc:
        raise _fail(str(exc)) from exc


@app.command("show")
def show_candidate(
    candidate_id: str = typer.Argument(..., help="Candidate identity."),
    workspace: Path | None = typer.Option(
        None, "--workspace", help="Project root containing .aibench/."
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Show a candidate beside its unchanged cited source spans for human review."""
    try:
        _ws, storage = _open_storage(workspace)
        try:
            item = storage.get_candidate(candidate_id)
            if item is None:
                raise AibenchError(f"no candidate with candidate_id={candidate_id!r}")
            events = storage.list_candidate_events(candidate_id)
        finally:
            storage.db.close()
        excerpts = [source_text_for_span(span) for span in item.source_spans]
        payload = {
            "candidate": item.model_dump(mode="json"),
            "source_excerpts": excerpts,
            "events": [event.model_dump(mode="json") for event in events],
        }
        if json_output:
            console.print_json(data=payload)
        else:
            console.print(f"[bold]{escape(item.candidate_id)}[/bold] — {item.status.value}")
            console.print(f"Input: {escape(str(item.case.input))}")
            console.print(
                "Expected answer (judge-only): "
                + escape(
                    (item.case.reference.answer if item.case.reference else None) or "(missing)"
                )
            )
            for span, excerpt in zip(item.source_spans, excerpts, strict=True):
                console.print(
                    f"Source: {escape(span.source_ref)} lines {span.start_line}-{span.end_line}"
                )
                console.print(f"  {escape(excerpt)}")
            for event in events:
                console.print(f"  review event: {event.kind} by {escape(event.actor)}")
    except (AibenchError, OSError) as exc:
        raise _fail(str(exc)) from exc


@app.command("review")
def review_candidate(
    candidate_id: str = typer.Argument(..., help="Candidate identity."),
    reviewer: str = typer.Option(..., "--reviewer", help="Human reviewer identity."),
    decision: str = typer.Option(
        ..., "--decision", help="source_verified, human_reviewed, or reject."
    ),
    note: str = typer.Option(..., "--note", help="Short human decision record."),
    workspace: Path | None = typer.Option(
        None, "--workspace", help="Project root containing .aibench/."
    ),
) -> None:
    """Append an explicit human review decision. Promotion remains a separate action."""
    try:
        _ws, storage = _open_storage(workspace)
        try:
            candidate = record_candidate_review(
                storage, candidate_id, reviewer=reviewer, decision=decision, note=note
            )
        finally:
            storage.db.close()
        console.print(
            f"{escape(candidate.candidate_id)}: {candidate.status.value} "
            f"({candidate.case.reference.status.value if candidate.case.reference else 'no reference'})"
        )
    except (AibenchError, OSError, ValueError) as exc:
        raise _fail(str(exc)) from exc


@app.command("verify")
def verify_candidate(
    candidate_id: str = typer.Argument(..., help="Candidate identity."),
    actor: str = typer.Option("local", "--actor", help="Identity for this executable check."),
    workspace: Path | None = typer.Option(
        None, "--workspace", help="Project root containing .aibench/."
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Run the strict source_quote_presence_v1 oracle and record its result."""
    try:
        _ws, storage = _open_storage(workspace)
        try:
            candidate = record_candidate_executable_check(storage, candidate_id, actor=actor)
        finally:
            storage.db.close()
        payload = {
            "candidate_id": candidate.candidate_id,
            "status": candidate.status.value,
            "reference_status": candidate.case.reference.status.value
            if candidate.case.reference
            else None,
            "verification": candidate.verifications[-1].model_dump(mode="json"),
        }
        if json_output:
            console.print_json(data=payload)
        else:
            console.print(
                f"{escape(candidate.candidate_id)}: {candidate.verifications[-1].outcome} "
                f"({candidate.verifications[-1].verifier_id})"
            )
    except (AibenchError, OSError, ValueError) as exc:
        raise _fail(str(exc)) from exc


@app.command("promote")
def promote(
    pool_id: str = typer.Argument(..., help="Candidate pool identity."),
    output: Path = typer.Argument(
        ..., help="New .jsonl dataset path; existing files are never replaced."
    ),
    candidates: list[str] = typer.Option(
        ..., "--candidate", help="Candidate ID to promote (repeat for more)."
    ),
    actor: str = typer.Option(
        "local", "--actor", help="Identity recording the explicit promotion."
    ),
    workspace: Path | None = typer.Option(
        None, "--workspace", help="Project root containing .aibench/."
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Promote only reviewed/verified candidates into a new ordinary benchmark JSONL file."""
    try:
        _ws, storage = _open_storage(workspace)
        try:
            path, promoted = promote_candidates(
                storage, pool_id, tuple(candidates), output, actor=actor
            )
        finally:
            storage.db.close()
        payload = {
            "pool_id": pool_id,
            "output": str(path.resolve()),
            "case_count": len(promoted),
            "candidate_ids": [item.candidate_id for item in promoted],
            "reference_statuses": [
                item.case.reference.status.value for item in promoted if item.case.reference
            ],
        }
        if json_output:
            console.print_json(data=payload)
        else:
            console.print(f"promoted {len(promoted)} case(s) to {escape(str(path))}")
    except (AibenchError, OSError, ValueError) as exc:
        raise _fail(str(exc)) from exc
