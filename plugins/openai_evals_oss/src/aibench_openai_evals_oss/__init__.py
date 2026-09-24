"""openai/evals (open-source framework) bridge for aibench (§11A, Prompt 17).

A separate plugin from the hosted Evals API bridge (`aibench-openai-evals-api`): different
package, dependencies, identity and capabilities.
"""

from aibench_openai_evals_oss.replay import REPLAY_EVALUATORS

EVALUATORS = REPLAY_EVALUATORS
