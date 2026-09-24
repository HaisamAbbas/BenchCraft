"""Hosted OpenAI Evals API bridge for aibench (§11B, Prompt 17).

A separate plugin from the open-source openai/evals bridge (`aibench-openai-evals-oss`):
different package, dependencies (the official `openai` SDK), identity and capabilities.
"""

from aibench_openai_evals_api.criterion import Criterion

EVALUATORS = (Criterion,)
