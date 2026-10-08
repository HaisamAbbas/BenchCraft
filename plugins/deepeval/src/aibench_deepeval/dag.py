"""DeepEval's DAGMetric, with the graph written in the plan as plain JSON.

A DAG metric is a small decision tree a judge walks: task nodes extract something from the
answer, judgement nodes answer a yes/no or multiple-choice question about it, and the verdict
node the judge lands on gives the score (0 to 10, reported as 0 to 1) or hands over to a
G-Eval for the final grade.

The graph is data, not code, and it is checked here before DeepEval sees it:

* only the four node types and the keys each one needs; anything else is refused,
* a verdict's child is another node or a G-Eval, never a `metric` child: upstream builds
  those with their own default model, which would send the case to a provider other than the
  judge the plan configured (a G-Eval child is rebuilt with the DAG's judge, so its `model`
  is ignored, and is refused all the same),
* at most `MAX_NODES` nodes, `MAX_DEPTH` deep, every text at most `MAX_TEXT` characters,
* every field a node shows the judge is one the plan names in `evaluation_params`, so the
  harness knows which case fields to send to the worker,
* mistakes upstream only reports while judging (a starting node that reads nothing, a yes/no
  node without both answers) are reported before any case runs.

`deepeval.conversational_dag` is the same graph over a conversation (DeepEval's
ConversationalDAGMetric): nodes read conversation fields (`role`, `content`, ...) and may look
at a `turn_window` [first, last] of turns; each turn is scored on the conversation so far.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

from aibench.core.models import EvaluatorManifest
from aibench.evaluators.protocol import EvaluationView
from aibench_deepeval.conversational import (
    _GEVAL_EXTRA,
    ConversationalMetric,
    ConversationSpec,
)
from aibench_deepeval.conversational import _manifest as _conversation_manifest
from aibench_deepeval.judges import JUDGE_SCHEMA, build_judge
from aibench_deepeval.metrics import (
    FIELDS,
    PINNED_DEEPEVAL,
    DeepEvalMetric,
    Spec,
    _manifest,
)

MAX_NODES = 40
MAX_DEPTH = 8
MAX_TEXT = 4000
MAX_TURN = 1000

# Fields a single-turn node may show the judge; the question and the answer always.
DAG_FIELDS = ("input", "actual_output", "expected_output", "retrieval_context", "context")
_EXTRA_FIELDS = tuple(name for name in DAG_FIELDS if name not in ("input", "actual_output"))
# A conversation's: each turn's role and content always, and what the plan names besides.
# Upstream's conversation nodes read their fields off each message (Turn), so only what a
# message carries: a conversation-level field such as expected_outcome failed while judging.
_CONVERSATION_EXTRA = ("retrieval_context", "tools_called")
CONVERSATION_FIELDS = ("role", "content", *_CONVERSATION_EXTRA)

_NODE_KEYS = {
    "TaskNode": {"type", "instructions", "output_label", "label", "evaluation_params", "children"},
    "BinaryJudgementNode": {"type", "criteria", "label", "evaluation_params", "children"},
    "NonBinaryJudgementNode": {"type", "criteria", "label", "evaluation_params", "children"},
    "VerdictNode": {"type", "verdict", "score", "child"},
}
_REQUIRED = {
    "TaskNode": ("instructions", "output_label"),
    "BinaryJudgementNode": ("criteria",),
    "NonBinaryJudgementNode": ("criteria",),
    "VerdictNode": ("verdict",),
}
_GEVAL_KEYS = {"type", "name", "criteria", "evaluation_steps", "evaluation_params"}


def validate_dag(
    document: Any, allowed_fields: tuple[str, ...] = DAG_FIELDS, *, multiturn: bool = False
) -> list[str]:
    """Every problem with a DAG document, or an empty list when it is safe to build."""
    if not isinstance(document, Mapping) or set(document) != {"nodes"}:
        return ["the DAG must be an object with exactly one key, 'nodes'"]
    nodes = document["nodes"]
    if not isinstance(nodes, Mapping) or not nodes:
        return ["'nodes' must be a non-empty object of node id -> node"]
    if len(nodes) > MAX_NODES:
        return [f"the DAG has {len(nodes)} nodes; at most {MAX_NODES} are allowed"]
    problems: list[str] = []
    children: dict[str, list[str]] = {}
    for node_id, node in nodes.items():
        problems += _check_node(str(node_id), node, nodes, allowed_fields, multiturn)
        children[str(node_id)] = _children_of(node)
    if problems:
        return problems
    referenced = {child for kids in children.values() for child in kids}
    roots = [node_id for node_id in children if node_id not in referenced]
    if not roots:
        return ["every node is some node's child: the graph has no root (a cycle)"]
    for root in roots:  # a node with no parent has nothing to read unless it is told what
        node = nodes[root]
        if node.get("type") != "VerdictNode" and not node.get("evaluation_params"):
            return [
                (
                    f"node '{root}' starts the graph, so it needs 'evaluation_params' "
                    f"(the fields it reads, for example {list(allowed_fields[:2])})"
                )
            ]
    depth = _depth(children, roots)
    if depth is None:
        return ["the graph has a cycle"]
    if depth > MAX_DEPTH:
        return [f"the DAG is {depth} levels deep; at most {MAX_DEPTH} are allowed"]
    return []


def _children_of(node: Any) -> list[str]:
    if not isinstance(node, Mapping):
        return []
    if node.get("type") == "VerdictNode":
        child = node.get("child")
        if isinstance(child, Mapping) and child.get("type") == "node":
            return [str(child.get("ref"))]
        return []
    return [str(c) for c in node.get("children") or []]


def _check_text(where: str, value: Any) -> list[str]:
    if not isinstance(value, str) or not value.strip():
        return [f"{where} must be a non-empty string"]
    if len(value) > MAX_TEXT:
        return [f"{where} is {len(value)} characters; at most {MAX_TEXT} are allowed"]
    return []


def _check_params(where: str, value: Any, allowed_fields: tuple[str, ...]) -> list[str]:
    if not isinstance(value, list) or not value:
        return [f"{where} must be a non-empty list of field names"]
    bad = [v for v in value if v not in allowed_fields]
    if bad:
        return [
            (f"{where} names {bad}; the plan's evaluation_params allow only {list(allowed_fields)}")
        ]
    return []


def _check_window(where: str, window: Any) -> list[str]:
    if (
        not isinstance(window, list)
        or len(window) != 2
        or not all(isinstance(t, int) and not isinstance(t, bool) for t in window)
        or not 0 <= window[0] < window[1] <= MAX_TURN
    ):
        return [
            (
                f"{where}: 'turn_window' must be [first, last] message numbers, "
                f"0 <= first < last <= {MAX_TURN} (0 is the first user message, 1 the "
                "first answer)"
            )
        ]
    return []


def window_end(document: Mapping[str, Any]) -> int | None:
    """The last message any node's `turn_window` reaches, or None without windows."""
    ends = [
        node["turn_window"][1]
        for node in document["nodes"].values()
        if isinstance(node, Mapping) and node.get("turn_window")
    ]
    return max(ends) if ends else None


def _check_node(
    node_id: str,
    node: Any,
    nodes: Mapping[str, Any],
    allowed_fields: tuple[str, ...],
    multiturn: bool,
) -> list[str]:
    where = f"node '{node_id}'"
    if not isinstance(node, Mapping) or node.get("type") not in _NODE_KEYS:
        return [f"{where}: 'type' must be one of {sorted(_NODE_KEYS)}"]
    kind = str(node["type"])
    allowed_keys = set(_NODE_KEYS[kind])
    if multiturn and kind != "VerdictNode":
        allowed_keys.add("turn_window")
    problems = [
        f"{where}: key '{key}' is not allowed on a {kind}"
        for key in node
        if key not in allowed_keys
    ]
    for key in _REQUIRED[kind]:
        if key not in node:
            problems.append(f"{where}: a {kind} needs '{key}'")
    for key in ("instructions", "output_label", "criteria", "label"):
        if key in node:
            problems += _check_text(f"{where}: '{key}'", node[key])
    if "evaluation_params" in node:
        problems += _check_params(
            f"{where}: 'evaluation_params'", node["evaluation_params"], allowed_fields
        )
    if "turn_window" in node and "turn_window" in allowed_keys:
        problems += _check_window(where, node["turn_window"])
    if kind == "VerdictNode":
        return problems + _check_verdict(where, node, nodes, allowed_fields)
    if kind == "BinaryJudgementNode":
        verdicts = [
            nodes[c].get("verdict")
            for c in node.get("children") or []
            if c in nodes and isinstance(nodes[c], Mapping)
        ]
        if len(node.get("children") or []) != 2 or sorted(map(str, verdicts)) != ["False", "True"]:
            problems.append(
                f"{where}: a BinaryJudgementNode needs exactly two verdict children, one "
                "with verdict true and one with verdict false"
            )
    for child in node.get("children") or []:
        if child not in nodes:
            problems.append(f"{where}: child '{child}' is not a node")
        elif isinstance(nodes[child], Mapping) and nodes[child].get("type") == "VerdictNode":
            if kind == "TaskNode":
                problems.append(f"{where}: a TaskNode cannot have a VerdictNode child")
        elif kind != "TaskNode" and isinstance(nodes[child], Mapping):
            problems.append(f"{where}: a judgement node's children must be verdict nodes")
    return problems


def _check_verdict(
    where: str, node: Mapping[str, Any], nodes: Mapping[str, Any], allowed_fields: tuple[str, ...]
) -> list[str]:
    problems: list[str] = []
    if not isinstance(node.get("verdict"), (bool, str)):
        problems.append(f"{where}: 'verdict' must be true/false or a string")
    has_score, has_child = node.get("score") is not None, node.get("child") is not None
    if has_score == has_child:
        return [*problems, f"{where}: a VerdictNode needs exactly one of 'score' and 'child'"]
    if has_score:
        score = node["score"]
        if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 10:
            problems.append(f"{where}: 'score' must be a whole number from 0 to 10")
        return problems
    child = node["child"]
    if not isinstance(child, Mapping):
        return [*problems, f"{where}: 'child' must be an object"]
    if child.get("type") == "node":
        if child.get("ref") not in nodes:
            problems.append(f"{where}: child ref '{child.get('ref')}' is not a node")
        return problems
    if child.get("type") != "geval":
        return [
            *problems,
            (
                f"{where}: a child may be a node or a geval, not '{child.get('type')}' (a "
                "'metric' child runs on its own default model, not the plan's judge)"
            ),
        ]
    problems += [
        f"{where}: geval key '{key}' is not allowed (the DAG's own judge grades it)"
        for key in child
        if key not in _GEVAL_KEYS
    ]
    if not (child.get("criteria") or child.get("evaluation_steps")):
        problems.append(f"{where}: a geval child needs 'criteria' or 'evaluation_steps'")
    for key in ("name", "criteria"):
        if key in child:
            problems += _check_text(f"{where}: geval '{key}'", child[key])
    if "evaluation_params" in child:
        problems += _check_params(
            f"{where}: geval 'evaluation_params'", child["evaluation_params"], allowed_fields
        )
    return problems


def _depth(children: dict[str, list[str]], roots: list[str]) -> int | None:
    """Longest root-to-leaf path in nodes, or None for a cycle."""
    memo: dict[str, int] = {}
    visiting: set[str] = set()

    def walk(node_id: str) -> int | None:
        if node_id in memo:
            return memo[node_id]
        if node_id in visiting:
            return None
        visiting.add(node_id)
        deepest = 0
        for child in children.get(node_id, []):
            below = walk(child)
            if below is None:
                return None
            deepest = max(deepest, below)
        visiting.discard(node_id)
        memo[node_id] = deepest + 1
        return deepest + 1

    levels = [walk(root) for root in roots]
    return None if any(level is None for level in levels) else max(levels)  # type: ignore[type-var]


def build_graph(document: Mapping[str, Any], judge: Any, *, multiturn: bool) -> Any:
    """A fresh graph for one case: a graph keeps what the judge decided on its nodes. Upstream
    builds each G-Eval child with DeepEval's default model (an OpenAI key); here it is built
    with the plan's judge."""
    import deepeval.metrics.dag.serialization.serialization as upstream
    from deepeval.metrics import ConversationalGEval, GEval
    from deepeval.metrics.dag import dag_from_dict
    from deepeval.test_case import MultiTurnParams, SingleTurnParams

    def build_geval(spec: dict[str, Any], multiturn: bool) -> Any:
        if multiturn:
            cls: Any = ConversationalGEval
            params: Any = MultiTurnParams
            default = ("role", "content")
        else:
            cls, params, default = GEval, SingleTurnParams, ("input", "actual_output")
        return cls(
            name=spec.get("name") or "decision tree step",
            criteria=spec.get("criteria"),
            evaluation_steps=spec.get("evaluation_steps"),
            evaluation_params=[params(name) for name in spec.get("evaluation_params") or default],
            model=judge,
        )

    original = upstream._build_geval
    upstream._build_geval = build_geval  # type: ignore[assignment]
    try:  # no await between the two lines: nothing else sees the patched function
        return dag_from_dict(copy.deepcopy(dict(document)), multiturn=multiturn)
    finally:
        upstream._build_geval = original  # type: ignore[assignment]


_DAG_SCHEMA = {
    "type": "object",
    "required": ["nodes"],
    "additionalProperties": False,
    "properties": {"nodes": {"type": "object", "minProperties": 1, "maxProperties": MAX_NODES}},
}
_TREE_LIMITS = (
    "The tree is part of the metric: runs with different trees are not comparable.",
    (
        f"The tree is limited to {MAX_NODES} nodes and {MAX_DEPTH} levels, with only task, "
        "judgement and verdict nodes and G-Eval children."
    ),
)

DAG_SPEC = Spec(
    "dag",
    "DAGMetric",
    "a judge walks a decision tree you write and the verdict it reaches gives the score",
    ("input", "actual_output"),
    ("custom_criteria",),
)


class Dag(DeepEvalMetric):
    """The DAG is part of the metric's identity: a different tree is a different metric."""

    spec = DAG_SPEC
    manifest = EvaluatorManifest(
        **{
            **_manifest(DAG_SPEC).model_dump(),
            "description": (
                f"DeepEval {PINNED_DEEPEVAL} DAGMetric: a judge walks the decision tree the "
                "plan states and the verdict it reaches gives the score (0 to 1)."
            ),
            "limitations": (
                "Judge-dependent: scores from different judge models are not comparable.",
                *_TREE_LIMITS,
            ),
            "parameter_requirements": {
                "evaluation_params": {
                    name: {"path": FIELDS[name][0], "non_empty": FIELDS[name][1]}
                    for name in _EXTRA_FIELDS
                }
            },
            "parameters_schema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["judge", "dag"],
                "properties": {
                    "judge": JUDGE_SCHEMA,
                    "name": {"type": "string", "minLength": 1, "maxLength": 80},
                    "dag": _DAG_SCHEMA,
                    "evaluation_params": {
                        "type": "array",
                        "uniqueItems": True,
                        "items": {"enum": list(_EXTRA_FIELDS)},
                    },
                },
            },
        }
    )

    def _allowed_fields(self) -> tuple[str, ...]:
        return ("input", "actual_output", *(self.params.get("evaluation_params") or ()))

    async def prepare(self, params: Any) -> None:
        self.params = dict(params)
        problems = validate_dag(self.params.get("dag"), self._allowed_fields())
        if problems:
            raise ValueError("the DAG is not valid: " + "; ".join(problems))
        await super().prepare(params)
        # Build once now so a graph upstream refuses fails here, not on the first case.
        build_graph(self.params["dag"], build_judge(self.params["judge"]), multiturn=False)

    def _fields(self) -> tuple[str, ...]:
        return tuple(name for name in DAG_FIELDS if name in self._allowed_fields())

    def _new_metric(self, judge: Any) -> Any:
        from deepeval.metrics import DAGMetric

        return DAGMetric(
            name=self.params.get("name") or "decision tree",
            dag=build_graph(self.params["dag"], judge, multiturn=False),
            model=judge,
            threshold=0.5,
            include_reason=True,
            async_mode=True,
            strict_mode=False,
            verbose_mode=False,
        )


# --------------------------------------------------------------------------- conversations

CONVERSATIONAL_DAG_SPEC = ConversationSpec(
    "conversational_dag",
    "ConversationalDAGMetric",
    "a judge walks a decision tree you write over the conversation",
    ("custom_criteria",),
)


class ConversationalDag(ConversationalMetric):
    """The DAG over a conversation: nodes read the turns' role and content, and the fields the
    plan names in `evaluation_params`; each may narrow to a `turn_window`."""

    spec = CONVERSATIONAL_DAG_SPEC
    manifest = EvaluatorManifest(
        **{
            **_conversation_manifest(CONVERSATIONAL_DAG_SPEC).model_dump(),
            "description": (
                f"DeepEval {PINNED_DEEPEVAL} ConversationalDAGMetric: a judge walks the "
                "decision tree the plan states over the conversation up to each turn (0 to 1)."
            ),
            "limitations": (
                "Judge-dependent: scores from different judge models are not comparable.",
                *_TREE_LIMITS,
                (
                    "Each turn is scored on the conversation so far; an episode's last turn "
                    "carries the whole conversation's score."
                ),
                (
                    "Nodes read what each message carries (role, content, retrieved passages, "
                    "tool calls), not the expected outcome: upstream reads node fields per "
                    "message. A turn_window counts messages, user and assistant alike; a turn "
                    "whose conversation does not reach it yet is not applicable."
                ),
            ),
            "parameter_requirements": {
                "evaluation_params": {k: _GEVAL_EXTRA[k] for k in _CONVERSATION_EXTRA}
            },
            "parameters_schema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["judge", "dag"],
                "properties": {
                    "judge": JUDGE_SCHEMA,
                    "name": {"type": "string", "minLength": 1, "maxLength": 80},
                    "dag": _DAG_SCHEMA,
                    "evaluation_params": {
                        "type": "array",
                        "uniqueItems": True,
                        "items": {"enum": list(_CONVERSATION_EXTRA)},
                    },
                },
            },
        }
    )

    def _allowed_fields(self) -> tuple[str, ...]:
        return ("role", "content", *(self.params.get("evaluation_params") or ()))

    def _reads(self) -> tuple[bool, bool, bool]:
        chosen = self._allowed_fields()
        return (
            "retrieval_context" in chosen,
            "tools_called" in chosen,
            "expected_outcome" in chosen,
        )

    def _test_case(self, view: EvaluationView) -> tuple[Any | None, str | None]:
        """A turn is scored on the conversation so far, so an early turn may not reach a node's
        window yet: upstream fails such a case, here it is not applicable."""
        test_case, not_applicable = super()._test_case(view)
        end = window_end(self.params["dag"])
        if test_case is not None and end is not None and end >= len(test_case.turns):
            return None, f"turn_window_beyond_conversation:{len(test_case.turns)}"
        return test_case, not_applicable

    async def prepare(self, params: Any) -> None:
        self.params = dict(params)
        problems = validate_dag(self.params.get("dag"), self._allowed_fields(), multiturn=True)
        if problems:
            raise ValueError("the DAG is not valid: " + "; ".join(problems))
        await super().prepare(params)
        build_graph(self.params["dag"], build_judge(self.params["judge"]), multiturn=True)

    def _new_metric(self, judge: Any) -> Any:
        from deepeval.metrics import ConversationalDAGMetric

        return ConversationalDAGMetric(
            name=self.params.get("name") or "decision tree",
            dag=build_graph(self.params["dag"], judge, multiturn=True),
            model=judge,
            threshold=0.5,
            include_reason=True,
            async_mode=True,
            strict_mode=False,
            verbose_mode=False,
        )
