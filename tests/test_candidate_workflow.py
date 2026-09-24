"""Candidate generation, evidence and promotion boundaries (18-T1/2)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aibench.cli.main import app
from aibench.core.errors import ValidationError
from aibench.core.models import CandidateStatus, ReferenceStatus
from aibench.datasets.candidates import (
    CandidateGenerationError,
    candidate_for_promotion,
    generate_candidate_pool,
)
from aibench.planning.planner import ModelReply, ToolCall
from aibench.services.candidates import (
    promote_candidates,
    record_candidate_executable_check,
    record_candidate_review,
)
from aibench.storage.db import Database
from aibench.storage.repositories import Storage

ANSWER = "Refunds may be requested within 30 days of purchase."
cli = CliRunner()


class FakeProvider:
    name = "fake"
    model = "deterministic-candidates-v1"

    def __init__(self, cases: list[dict[str, str]] | None = None) -> None:
        self.cases = cases or [
            {
                "input": "How long do I have to request a refund?",
                "expected_answer": ANSWER,
                "source_id": "source_1",
                "source_quote": ANSWER,
            }
        ]
        self.calls: list[tuple[list[dict[str, Any]], list[dict[str, Any]]]] = []

    def complete(self, messages, tools):
        self.calls.append((messages, tools))
        return ModelReply(
            text=None,
            tool_calls=(
                ToolCall("fake-call", "write_candidates", json.dumps({"cases": self.cases})),
            ),
            prompt_tokens=31,
            completion_tokens=19,
        )


@pytest.fixture
def candidate_job(tmp_path: Path):
    source = tmp_path / "refunds.md"
    source.write_text("# Refund policy\n\n" + ANSWER + " Items must be unused.", encoding="utf-8")
    provider = FakeProvider()
    manifest, candidates = generate_candidate_pool(
        [source], provider, pool_id="pool-test", source_split="development", max_candidates=1
    )
    storage = Storage(Database.open_in_memory())
    storage.commit_candidate_pool(manifest, candidates)
    try:
        yield source, provider, manifest, candidates[0], storage
    finally:
        storage.db.close()


def test_generation_is_bounded_and_records_exact_source_and_provider_provenance(
    candidate_job,
) -> None:
    source, provider, manifest, candidate, _storage = candidate_job
    assert len(provider.calls) == 1
    assert provider.calls[0][1][0]["function"]["parameters"]["properties"]["cases"]["maxItems"] == 1
    assert "Refund policy" in provider.calls[0][0][1]["content"]
    assert manifest.split_id == "development"
    assert manifest.generator_identity == "fake:deterministic-candidates-v1"
    assert manifest.prompt_hash.startswith("sha256:")
    assert candidate.status is CandidateStatus.CANDIDATE
    assert candidate.case.reference.status is ReferenceStatus.SYNTHETIC_UNVERIFIED
    # Candidate pools are not accepted by `summarize_dataset` or inserted into trusted
    # dataset/case repositories until a separate promotion operation writes JSONL.
    assert candidate.source_spans[0].source_ref == str(source.resolve())
    assert candidate.source_spans[0].start_line == 3
    assert (
        source.read_bytes().decode("utf-8")[
            candidate.source_spans[0].start_offset : candidate.source_spans[0].end_offset
        ]
        == ANSWER
    )
    projection = candidate.case.application_input_projection()
    assert projection["input"] == "How long do I have to request a refund?"
    assert "reference" not in projection
    assert ANSWER not in json.dumps(projection)


def test_exact_duplicate_sources_are_recorded_and_sent_once(tmp_path: Path) -> None:
    first, duplicate = tmp_path / "a.md", tmp_path / "b.md"
    content = "Refunds may be requested within 30 days of purchase."
    first.write_text(content, encoding="utf-8")
    duplicate.write_text(content, encoding="utf-8")
    provider = FakeProvider()
    manifest, candidates = generate_candidate_pool(
        [first, duplicate], provider, pool_id="pool-duplicates", source_split="development"
    )
    assert len(provider.calls) == 1
    assert "source_2" not in provider.calls[0][0][1]["content"]
    assert manifest.sources[1].duplicate_of == str(first.resolve())
    assert len(candidates) == 1


def test_distinct_case_sensitive_paths_are_not_treated_as_the_same_source(
    tmp_path: Path,
) -> None:
    upper = tmp_path / "Policy.md"
    lower = tmp_path / "policy.md"
    upper.write_text("Refunds are available within 30 days.", encoding="utf-8")
    lower.write_text("Exchanges are available within 45 days.", encoding="utf-8")
    if upper.samefile(lower):
        pytest.skip("the test filesystem is case-insensitive")

    answer = "Exchanges are available within 45 days."
    provider = FakeProvider(
        [
            {
                "input": "How long for an exchange?",
                "expected_answer": answer,
                "source_id": "source_2",
                "source_quote": answer,
            }
        ]
    )
    manifest, candidates = generate_candidate_pool(
        [upper, lower], provider, pool_id="pool-case-sensitive", source_split="development"
    )

    assert len(manifest.sources) == 2
    assert all(source.duplicate_of is None for source in manifest.sources)
    assert len(candidates) == 1
    assert "source_2" in provider.calls[0][0][1]["content"]


def test_holdout_split_is_rejected_before_source_read_or_provider_call(tmp_path: Path) -> None:
    provider = FakeProvider()
    with pytest.raises(CandidateGenerationError, match="development split"):
        generate_candidate_pool(
            [tmp_path / "does-not-exist.md"],
            provider,
            pool_id="pool-blocked",
            source_split="holdout",
        )
    assert provider.calls == []


def test_candidate_cli_checks_model_egress_before_opening_workspace(tmp_path: Path) -> None:
    source = tmp_path / "policy.md"
    source.write_text(ANSWER, encoding="utf-8")
    config = tmp_path / "provider.json"
    config.write_text(
        json.dumps(
            {
                "kind": "openai_compatible",
                "base_url": "https://models.invalid/v1",
                "model": "candidate-model",
            }
        ),
        encoding="utf-8",
    )
    workspace = tmp_path / "project"
    result = cli.invoke(
        app,
        [
            "dataset",
            "candidates",
            "generate",
            str(source),
            "--split",
            "development",
            "--max-candidates",
            "1",
            "--provider-config",
            str(config),
            "--workspace",
            str(workspace),
            "--json",
        ],
    )
    assert result.exit_code == 2
    assert "not an approved destination" in result.output
    assert not (workspace / ".aibench").exists()


def test_unreviewed_synthetic_references_cannot_be_promoted(candidate_job, tmp_path: Path) -> None:
    _source, _provider, manifest, candidate, storage = candidate_job
    with pytest.raises(ValidationError, match="explicit human review"):
        candidate_for_promotion(candidate)
    output = tmp_path / "unreviewed.jsonl"
    with pytest.raises(ValidationError, match="explicit human review"):
        promote_candidates(
            storage, manifest.pool_id, (candidate.candidate_id,), output, actor="reviewer"
        )
    assert not output.exists()
    assert storage.get_candidate(candidate.candidate_id).status is CandidateStatus.CANDIDATE
    assert storage.conn.execute("SELECT count(*) FROM datasets").fetchone()[0] == 0
    assert storage.conn.execute("SELECT count(*) FROM cases").fetchone()[0] == 0
    assert [event.kind for event in storage.list_candidate_events(candidate.candidate_id)] == [
        "generated"
    ]


def test_human_review_and_promotion_are_separate_audited_actions(
    candidate_job, tmp_path: Path
) -> None:
    _source, _provider, manifest, candidate, storage = candidate_job
    reviewed = record_candidate_review(
        storage,
        candidate.candidate_id,
        reviewer="domain-reviewer",
        decision="human_reviewed",
        note="Checked against the cited refund policy and approved.",
    )
    assert reviewed.status is CandidateStatus.REVIEWED
    assert reviewed.case.reference.status is ReferenceStatus.HUMAN_REVIEWED
    assert [event.kind for event in storage.list_candidate_events(candidate.candidate_id)] == [
        "generated",
        "reviewed_human",
    ]

    output, promoted = promote_candidates(
        storage,
        manifest.pool_id,
        (candidate.candidate_id,),
        tmp_path / "reviewed.jsonl",
        actor="dataset-owner",
    )
    assert output.exists()
    assert promoted[0].status is CandidateStatus.PROMOTED
    assert storage.get_candidate(candidate.candidate_id).status is CandidateStatus.PROMOTED
    exported = json.loads(output.read_text(encoding="utf-8"))
    assert exported["reference"]["status"] == "human_reviewed"
    assert [event.kind for event in storage.list_candidate_events(candidate.candidate_id)] == [
        "generated",
        "reviewed_human",
        "promoted",
    ]


def test_cli_review_and_promotion_publish_a_new_dataset(candidate_job, tmp_path: Path) -> None:
    source, _provider, manifest, candidate, _storage = candidate_job
    # Move the in-memory records into the workspace the user-facing commands will open.
    workspace_storage = Storage(Database.open(tmp_path / ".aibench" / "aibench.db"))
    try:
        workspace_storage.commit_candidate_pool(manifest, [candidate])
    finally:
        workspace_storage.db.close()

    shown = cli.invoke(
        app,
        [
            "dataset",
            "candidates",
            "show",
            candidate.candidate_id,
            "--workspace",
            str(tmp_path),
            "--json",
        ],
    )
    assert shown.exit_code == 0, shown.output
    assert json.loads(shown.stdout)["source_excerpts"] == [ANSWER]

    reviewed = cli.invoke(
        app,
        [
            "dataset",
            "candidates",
            "review",
            candidate.candidate_id,
            "--reviewer",
            "domain-reviewer",
            "--decision",
            "source_verified",
            "--note",
            "The cited text supports the answer.",
            "--workspace",
            str(tmp_path),
        ],
    )
    assert reviewed.exit_code == 0, reviewed.output
    output = tmp_path / "promoted.jsonl"
    result = cli.invoke(
        app,
        [
            "dataset",
            "candidates",
            "promote",
            manifest.pool_id,
            str(output),
            "--candidate",
            candidate.candidate_id,
            "--actor",
            "dataset-owner",
            "--workspace",
            str(tmp_path),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["case_count"] == 1
    promoted = json.loads(output.read_text(encoding="utf-8"))
    assert promoted["provenance"]["reviewer_identity"] == "domain-reviewer"
    assert promoted["provenance"]["origin"] == "source_verified"
    assert source.exists()


def test_executable_oracle_requires_exact_supported_answer_and_records_failure(
    tmp_path: Path,
) -> None:
    source = tmp_path / "policy.md"
    source.write_text("Refunds may be requested within 30 days of purchase.", encoding="utf-8")
    provider = FakeProvider(
        [
            {
                "input": "Can I get a refund?",
                "expected_answer": "Refunds are always approved.",
                "source_id": "source_1",
                "source_quote": "Refunds may be requested within 30 days of purchase.",
            }
        ]
    )
    manifest, candidates = generate_candidate_pool(
        [source], provider, pool_id="pool-failed-oracle", source_split="development"
    )
    storage = Storage(Database.open_in_memory())
    storage.commit_candidate_pool(manifest, candidates)
    try:
        failed = record_candidate_executable_check(
            storage, candidates[0].candidate_id, actor="test-oracle"
        )
        assert failed.status is CandidateStatus.CANDIDATE
        assert failed.case.reference.status is ReferenceStatus.SYNTHETIC_UNVERIFIED
        assert failed.verifications[-1].outcome == "failed"
        with pytest.raises(ValidationError, match="explicit human review"):
            promote_candidates(
                storage,
                manifest.pool_id,
                (candidates[0].candidate_id,),
                tmp_path / "bad.jsonl",
                actor="owner",
            )
    finally:
        storage.db.close()


def test_passing_executable_oracle_unlocks_explicit_promotion(
    candidate_job, tmp_path: Path
) -> None:
    _source, _provider, manifest, candidate, storage = candidate_job
    verified = record_candidate_executable_check(
        storage, candidate.candidate_id, actor="source-quote-check"
    )
    assert verified.status is CandidateStatus.VERIFIED
    assert verified.case.reference.status is ReferenceStatus.EXECUTABLE_ORACLE
    assert verified.verifications[-1].verifier_id == "aibench.source_quote_presence.v1"
    output, _promoted = promote_candidates(
        storage,
        manifest.pool_id,
        (candidate.candidate_id,),
        tmp_path / "oracle.jsonl",
        actor="dataset-owner",
    )
    assert output.exists()
    assert (
        json.loads(output.read_text(encoding="utf-8"))["reference"]["status"] == "executable_oracle"
    )


def test_source_digest_change_blocks_review(candidate_job) -> None:
    source, _provider, _manifest, candidate, storage = candidate_job
    source.write_text("Changed after generation.", encoding="utf-8")
    with pytest.raises(ValidationError, match="changed since generation"):
        record_candidate_review(
            storage,
            candidate.candidate_id,
            reviewer="reviewer",
            decision="source_verified",
            note="Reviewed the source.",
        )
    assert storage.get_candidate(candidate.candidate_id).status is CandidateStatus.CANDIDATE
