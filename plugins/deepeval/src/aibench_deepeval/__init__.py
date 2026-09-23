"""aibench adapter for DeepEval. Runs only inside its own plugin environment, driven by the
aibench evaluation worker; DeepEval types never leave this package."""

from aibench_deepeval.faithfulness import Faithfulness

__version__ = "0.1.0"
EVALUATORS = (Faithfulness,)
