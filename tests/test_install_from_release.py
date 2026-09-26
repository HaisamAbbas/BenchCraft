"""Installed from a wheel, BenchCraft has no source checkout: plugin environments get the
release's wheels instead, chosen by exact name from its SHA256SUMS and verified against it
(never looked up by name on a public index)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from aibench import __version__
from aibench.services import plugins
from aibench.services.releases import ReleaseError, checksums, fetch, release_source, wheel_name


def _release(tmp_path: Path, files: dict[str, bytes]) -> Path:
    folder = tmp_path / "release"
    folder.mkdir()
    lines = []
    for name, data in files.items():
        (folder / name).write_bytes(data)
        lines.append(f"{hashlib.sha256(data).hexdigest()}  {name}")
    (folder / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return folder


CORE = f"aibench-{__version__}-py3-none-any.whl"
PLUGIN = "aibench_deepeval-0.2.0rc1-py3-none-any.whl"


def test_the_source_is_the_matching_github_release_unless_overridden(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("BENCHCRAFT_RELEASES", raising=False)
    assert release_source("0.3.0") == (
        "https://github.com/HaisamAbbas/BenchCraft/releases/download/v0.3.0"
    )
    monkeypatch.setenv("BENCHCRAFT_RELEASES", str(tmp_path))
    assert release_source("0.3.0") == str(tmp_path)


def test_wheels_are_chosen_by_exact_name_and_verified(tmp_path: Path) -> None:
    folder = _release(
        tmp_path,
        {CORE: b"core", PLUGIN: b"plugin", "aibench-9.9.9-py3-none-any.whl": b"other"},
    )
    listed = checksums(str(folder))
    assert wheel_name(listed, "aibench", __version__) == CORE
    assert wheel_name(listed, "aibench-deepeval") == PLUGIN
    with pytest.raises(ReleaseError, match="no wheel for aibench-ragas"):
        wheel_name(listed, "aibench-ragas")
    paths = fetch(str(folder), [CORE, PLUGIN], tmp_path / "out", listed)
    assert [p.read_bytes() for p in paths] == [b"core", b"plugin"]

    (folder / PLUGIN).write_bytes(b"tampered")
    with pytest.raises(ReleaseError, match="does not match the release's SHA256SUMS"):
        fetch(str(folder), [PLUGIN], tmp_path / "again", listed)
    assert not (tmp_path / "again" / PLUGIN).exists()


def test_a_release_without_checksums_is_refused(tmp_path: Path) -> None:
    (tmp_path / "SHA256SUMS").write_text("not a checksum line\n", encoding="utf-8")
    with pytest.raises(ReleaseError, match="no usable SHA256SUMS"):
        checksums(str(tmp_path))


def test_an_installed_package_installs_plugins_from_the_release(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    folder = _release(tmp_path, {CORE: b"core", PLUGIN: b"plugin"})
    monkeypatch.setenv("BENCHCRAFT_RELEASES", str(folder))
    monkeypatch.setattr(plugins, "_source_checkout", lambda _plugin: None)
    project = tmp_path / "project"
    project.mkdir()
    judge = {"kind": "openai_compatible", "base_url": "https://x.test/v1", "model": "m"}
    plan = plugins.plan_install("deepeval", project, policy_path=None, judge=judge)
    assert plan.source is None and plan.release == str(folder)
    assert plan.summary()["installs_from"] == f"release {folder}"

    requirements = plugins._adapter_requirements(plan, lambda _line: None)
    wheels = project / ".aibench" / "plugins" / "deepeval" / "wheels"
    assert requirements == [str(wheels / CORE), str(wheels / PLUGIN)]
    assert (wheels / PLUGIN).read_bytes() == b"plugin"


def test_a_checkout_still_installs_its_sources(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    judge = {"kind": "openai_compatible", "base_url": "https://x.test/v1", "model": "m"}
    plan = plugins.plan_install("deepeval", project, policy_path=None, judge=judge)
    assert plan.source is not None and plan.release is None  # this test runs from a clone
    requirements = plugins._adapter_requirements(plan, lambda _line: None)
    assert requirements == ["-e", str(plan.source), "-e", str(plan.source / "plugins/deepeval")]
    json.dumps(plan.summary())  # the preview stays serializable
