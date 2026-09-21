"""Export versioned JSON Schemas for all canonical models (01-T1)."""

from __future__ import annotations

import json
from pathlib import Path

from aibench.core.models import ALL_MODELS, SCHEMA_VERSION


def export_schemas(target_dir: Path) -> list[Path]:
    target_dir.mkdir(parents=True, exist_ok=True)
    version_dir = target_dir / SCHEMA_VERSION
    version_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for model in ALL_MODELS:
        schema = model.model_json_schema()
        schema["$id"] = f"aibench/{SCHEMA_VERSION}/{model.__name__}.json"
        schema["$schemaVersion"] = SCHEMA_VERSION
        out_path = version_dir / f"{model.__name__}.json"
        out_path.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        written.append(out_path)
    return written


def main() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    written = export_schemas(repo_root / "schemas")
    for path in written:
        print(f"wrote {path.relative_to(repo_root)}")


if __name__ == "__main__":
    main()
