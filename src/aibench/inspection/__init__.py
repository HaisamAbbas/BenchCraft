"""Repository inspection and evidence-backed application profiles."""

from importlib import import_module

__all__ = [
    "ApplicationProfile",
    "ApplicationProfiler",
    "CandidateValidationBudget",
    "CodebaseInspector",
    "DatasetSelectionDecision",
    "DiscoveryResult",
    "InspectionBudget",
    "RepositoryCandidateInventory",
    "RepositoryDatasetCandidate",
    "SourceInspection",
    "SupportedFormat",
    "compatible_dataset_groups",
    "discover_repository_candidates",
    "select_unique_dataset",
]

_EXPORTS = {
    "ApplicationProfile": ("aibench.inspection.profile", "ApplicationProfile"),
    "ApplicationProfiler": ("aibench.inspection.profile", "ApplicationProfiler"),
    "CandidateValidationBudget": (
        "aibench.inspection.candidates",
        "CandidateValidationBudget",
    ),
    "CodebaseInspector": ("aibench.inspection.source", "CodebaseInspector"),
    "DatasetSelectionDecision": (
        "aibench.inspection.candidates",
        "DatasetSelectionDecision",
    ),
    "DiscoveryResult": ("aibench.inspection.source", "DiscoveryResult"),
    "InspectionBudget": ("aibench.inspection.source", "InspectionBudget"),
    "RepositoryCandidateInventory": (
        "aibench.inspection.candidates",
        "RepositoryCandidateInventory",
    ),
    "RepositoryDatasetCandidate": (
        "aibench.inspection.candidates",
        "RepositoryDatasetCandidate",
    ),
    "SourceInspection": ("aibench.inspection.source", "SourceInspection"),
    "SupportedFormat": ("aibench.inspection.source", "SupportedFormat"),
    "compatible_dataset_groups": (
        "aibench.inspection.candidates",
        "compatible_dataset_groups",
    ),
    "discover_repository_candidates": (
        "aibench.inspection.candidates",
        "discover_repository_candidates",
    ),
    "select_unique_dataset": ("aibench.inspection.candidates", "select_unique_dataset"),
}


def __getattr__(name: str):
    try:
        module, member = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    return getattr(import_module(module), member)
