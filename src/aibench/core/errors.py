"""Typed errors for the aibench core. No evaluator/UI framework dependencies here."""

from __future__ import annotations


class AibenchError(Exception):
    """Base class for all aibench domain errors."""


class ValidationError(AibenchError):
    """A dataset, config, or plan input failed schema/semantic validation."""

    def __init__(self, message: str, *, line: int | None = None, field: str | None = None) -> None:
        self.line = line
        self.field = field
        location = ""
        if line is not None:
            location += f" (line {line})"
        if field is not None:
            location += f" [field={field}]"
        super().__init__(f"{message}{location}")


class PolicyError(AibenchError):
    """An action was rejected by policy independent of any LLM decision."""


class ConfigError(AibenchError):
    """Configuration resolution or precedence failed."""


class WorkspaceTooNew(ConfigError):
    """The workspace database was migrated by a newer aibench than this one. Writing to a
    schema this version does not know could corrupt it, so the workspace is refused."""


class ConflictError(AibenchError):
    """A logical commit collided with an existing record under the same identity but with
    different content. A duplicate commit of *identical* content is idempotent and must not
    raise this; only a genuine mismatch (same key, different data) does."""
