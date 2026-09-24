"""aibench adapter for the pinned Ragas package.

The adapter is loaded by aibench's isolated evaluation worker.  Ragas itself is
intentionally imported lazily by :mod:`aibench_ragas.faithfulness`, after the
worker has set ``RAGAS_DO_NOT_TRACK=true``.
"""

from aibench_ragas._version import __version__
from aibench_ragas.faithfulness import Faithfulness

__all__ = ["EVALUATORS", "Faithfulness", "__version__"]
EVALUATORS = (Faithfulness,)
