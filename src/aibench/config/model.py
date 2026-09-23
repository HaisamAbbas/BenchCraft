"""Configuration models (§13, §16). No secret values are ever stored directly; only
references that are resolved at use time by a secret-handling layer added in a later
prompt."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

RESERVED_POLICY_KEYS = {"policy", "credentials", "network", "budgets", "effects", "approvals"}


class SecretRef(BaseModel):
    """A reference to a secret, e.g. `env:OPENAI_API_KEY` or `keyring:aibench/judge`.
    Never the literal secret value."""

    model_config = ConfigDict(frozen=True)

    source: str  # "env" | "keyring" | ...
    name: str

    @classmethod
    def parse(cls, raw: str) -> SecretRef:
        if ":" not in raw:
            raise ValueError(f"secret reference must be 'source:name', got {raw!r}")
        source, name = raw.split(":", 1)
        return cls(source=source, name=name)

    def __str__(self) -> str:  # redacted by construction: never renders a value
        return f"{self.source}:{self.name}"


class AibenchConfig(BaseModel):
    """Project configuration resolved from defaults < config file < permitted env
    overrides < CLI flags."""

    model_config = ConfigDict(extra="forbid")

    project_root: str = "."
    dataset_path: str | None = None
    application_target: str | None = None
    policy_path: str | None = None
    plan_path: str | None = None  # the executable plan `aibench run` uses (Prompt 11)
    secrets: dict[str, SecretRef] = Field(default_factory=dict)
    extensions: dict[str, object] = Field(default_factory=dict)

    def redacted(self) -> dict[str, object]:
        """A dict safe to print or log: secret references show only source:name, never
        a resolved value (none is ever stored on this model in the first place)."""
        data = self.model_dump(mode="json")
        data["secrets"] = {k: str(v) for k, v in self.secrets.items()}
        return data
