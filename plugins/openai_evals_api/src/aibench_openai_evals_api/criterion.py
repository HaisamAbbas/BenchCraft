"""The metric identity of a hosted Evals API testing criterion (§11B).

Results come from a remote job (`aibench openai-evals-api submit/status/fetch`), one per
case and criterion, recorded with the job's remote identifiers. `consumes="remote_job"`:
there is no per-case evaluate call, and a plan cannot bind this metric.
"""

from __future__ import annotations

from aibench.core.models import (
    DecisionRule,
    EvaluatorManifest,
    FieldRequirement,
    MetricDirection,
)
from aibench.evaluators.protocol import (
    EvaluationOutcome,
    EvaluationView,
    Evaluator,
    EvaluatorContext,
)
from aibench_openai_evals_api import contract
from aibench_openai_evals_api._version import __version__

PLUGIN_ID = "aibench-openai-evals-api"


class Criterion(Evaluator):
    manifest = EvaluatorManifest(
        evaluator_id="openai_evals_api.criterion",
        version="1.0.0",
        plugin_id=PLUGIN_ID,
        plugin_version=__version__,
        package_name="openai",
        package_version=contract.PINNED_OPENAI,
        description=(
            "One testing criterion of a hosted OpenAI Evals API run, grading the recorded "
            "application output uploaded as a jsonl item: passed or failed, with the "
            "grader's score kept in the raw result."
        ),
        limitations=(
            (
                "Runs as a remote job with explicit data egress: case input, recorded output "
                "and reference answer are uploaded to the configured Evals API endpoint."
            ),
            (
                "Stored-output scoring only: the service never generates a replacement "
                "answer (no completions/responses data source, no {{sample.*}} templates)."
            ),
            (
                "Supported graders: string_check, text_similarity, and label_model (a model "
                "judge, which needs model evaluators allowed by the policy)."
            ),
            "Cost is not reported by the service for rule graders and stays unknown.",
        ),
        value_kind="boolean",
        direction=MetricDirection.HIGHER,
        aggregation="rate",
        requires=(FieldRequirement(path="execution.output", non_empty=False),),
        default_rule=DecisionRule(comparator="is_true"),
        parameters_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["criterion"],
            "properties": {"criterion": {"type": "object"}, "job_id": {"type": "string"}},
        },
        consumes="remote_job",
        uses_models=False,
        credentials=("OPENAI_API_KEY, from a policy-approved secret reference",),
        network_destinations=("the configured OpenAI Evals API base URL (policy-approved)",),
        internal_retries=0,
        internal_concurrency=1,
        requires_worker=True,
    )

    async def evaluate(self, view: EvaluationView, ctx: EvaluatorContext) -> EvaluationOutcome:
        return EvaluationOutcome.error(
            "remote_job: openai_evals_api results come from `aibench openai-evals-api fetch`, "
            "never from a per-case evaluation"
        )
