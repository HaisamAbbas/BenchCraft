"""aibench adapter for DeepEval. Runs only inside its own plugin environment, driven by the
aibench evaluation worker; DeepEval types never leave this package."""

from aibench_deepeval._version import __version__
from aibench_deepeval.arena import ArenaGEval
from aibench_deepeval.audio import AudioGEval
from aibench_deepeval.conversational import CONVERSATIONAL_METRICS, ConversationalGEval
from aibench_deepeval.dag import ConversationalDag, Dag
from aibench_deepeval.faithfulness import Faithfulness
from aibench_deepeval.image_metrics import IMAGE_EVALUATORS
from aibench_deepeval.metrics import METRICS, GEval
from aibench_deepeval.video_frames import VideoFramesGEval
from aibench_deepeval.vision import VisionGEval
from aibench_deepeval.voice import VOICE_EVALUATORS

__all__ = [
    "EVALUATORS",
    "IMAGE_EVALUATORS",
    "ArenaGEval",
    "AudioGEval",
    "ConversationalDag",
    "ConversationalGEval",
    "Dag",
    "Faithfulness",
    "GEval",
    "VideoFramesGEval",
    "VisionGEval",
    "__version__",
]
EVALUATORS = (
    Faithfulness,
    *METRICS,
    *IMAGE_EVALUATORS,
    *VOICE_EVALUATORS,
    GEval,
    VisionGEval,
    AudioGEval,
    VideoFramesGEval,
    Dag,
    *CONVERSATIONAL_METRICS,
    ConversationalGEval,
    ConversationalDag,
    ArenaGEval,
)
