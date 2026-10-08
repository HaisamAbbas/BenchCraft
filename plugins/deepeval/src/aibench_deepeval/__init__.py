"""aibench adapter for DeepEval. Runs only inside its own plugin environment, driven by the
aibench evaluation worker; DeepEval types never leave this package."""

from aibench_deepeval._version import __version__
from aibench_deepeval.conversational import CONVERSATIONAL_METRICS, ConversationalGEval
from aibench_deepeval.dag import ConversationalDag, Dag
from aibench_deepeval.faithfulness import Faithfulness
from aibench_deepeval.metrics import METRICS, GEval

__all__ = [
    "EVALUATORS",
    "ConversationalDag",
    "ConversationalGEval",
    "Dag",
    "Faithfulness",
    "GEval",
    "__version__",
]
EVALUATORS = (
    Faithfulness,
    *METRICS,
    GEval,
    Dag,
    *CONVERSATIONAL_METRICS,
    ConversationalGEval,
    ConversationalDag,
)
