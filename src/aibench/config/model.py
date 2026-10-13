"""Configuration models (§13, §16). No secret values are ever stored directly; only
references that are resolved at use time by a secret-handling layer added in a later
prompt."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from aibench.core.models import SecretRefStr

RESERVED_POLICY_KEYS = {"policy", "credentials", "network", "budgets", "effects", "approvals"}


class SecretRef(BaseModel):
    """A reference to a secret, currently `env:OPENAI_API_KEY`.
    Never the literal secret value; other sources need an implemented resolver first."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str  # currently "env"
    name: str = Field(min_length=1, pattern=r"^\S+$")

    @classmethod
    def parse(cls, raw: str) -> SecretRef:
        if ":" not in raw:
            raise ValueError("secret reference must use the 'source:name' format")
        source, name = raw.split(":", 1)
        return cls(source=source, name=name)

    def __str__(self) -> str:  # redacted by construction: never renders a value
        return f"{self.source}:{self.name}"


class PluginEnvironmentConfig(BaseModel):
    """A plugin environment the project uses (e.g. the one `aibench plugins install`
    creates): its interpreter, the secrets its workers receive, and default parameters for
    its evaluators by ID pattern (e.g. the judge for `deepeval.*`). Paths are relative to
    the config file. The policy must still allow the interpreter and the secrets."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    python: str
    paths: list[str] = Field(default_factory=list)
    secret_env: dict[str, SecretRefStr] = Field(default_factory=dict)  # NAME -> source:name
    startup_timeout_seconds: float = Field(default=120.0, gt=0, le=3_600)
    default_params: dict[str, dict[str, object]] = Field(default_factory=dict)


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
    plugin_environments: list[PluginEnvironmentConfig] = Field(default_factory=list)
    extensions: dict[str, object] = Field(default_factory=dict)

    def redacted(self) -> dict[str, object]:
        """A dict safe to print or log: secret references show only source:name, never
        a resolved value (none is ever stored on this model in the first place)."""
        data = self.model_dump(mode="json")
        data["secrets"] = {k: str(v) for k, v in self.secrets.items()}
        return data
