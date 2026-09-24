"""aibench adapter for DeepEval. Runs only inside its own plugin environment, driven by the
aibench evaluation worker; DeepEval types never leave this package."""

from aibench_deepeval._version import __version__
from aibench_deepeval.faithfulness import Faithfulness

__all__ = ["EVALUATORS", "Faithfulness", "__version__"]
EVALUATORS = (Faithfulness,)
