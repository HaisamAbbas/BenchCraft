"""BenchCraft's published release files: the wheels an installed copy fetches when there is
no source checkout (plugin environments, `install.ps1` / `install.sh`).

A release is a directory of files next to its `SHA256SUMS` (what `scripts/release_check.py`
writes and the release workflow attaches to a GitHub Release). Files are chosen by exact
name from that list and verified against it before use; nothing is looked up by package
name on a public index, so a same-named package published elsewhere cannot be picked up.

The source is `BENCHCRAFT_RELEASES` when set (a URL or a local directory holding the files,
e.g. `D:\\rc\\dist`), else the GitHub Release matching the installed version.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Callable
from pathlib import Path

from aibench import __version__
from aibench.core.errors import AibenchError

RELEASES_URL = "https://github.com/HaisamAbbas/BenchCraft/releases/download"
SOURCE_ENV = "BENCHCRAFT_RELEASES"
_SUMS = "SHA256SUMS"
_LINE = re.compile(r"^([0-9a-f]{64})\s+\*?(\S+)$")
_TIMEOUT = 120.0


class ReleaseError(AibenchError):
    pass


def release_source(version: str = __version__) -> str:
    """Where this version's release files are: the override, or its GitHub Release."""
    return os.environ.get(SOURCE_ENV) or f"{RELEASES_URL}/v{version}"


def _is_url(source: str) -> bool:
    return source.startswith(("https://", "http://"))


def _read(source: str, name: str) -> bytes:
    if not _is_url(source):
        path = Path(source) / name
        try:
            return path.read_bytes()
        except OSError as exc:
            raise ReleaseError(f"cannot read {path}: {exc}") from exc
    import httpx

    url = f"{source.rstrip('/')}/{name}"
    try:
        response = httpx.get(url, follow_redirects=True, timeout=_TIMEOUT)
    except httpx.HTTPError as exc:
        raise ReleaseError(f"cannot download {url}: {exc}") from exc
    if response.status_code != 200:
        raise ReleaseError(f"cannot download {url}: HTTP {response.status_code}")
    return response.content


def checksums(source: str) -> dict[str, str]:
    """The release's file names and their SHA-256 digests."""
    listed: dict[str, str] = {}
    for line in _read(source, _SUMS).decode("utf-8", "replace").splitlines():
        match = _LINE.match(line.strip())
        if match:
            listed[match.group(2)] = match.group(1)
    if not listed:
        raise ReleaseError(f"{source} has no usable {_SUMS}")
    return listed


def wheel_name(listed: dict[str, str], distribution: str, version: str | None = None) -> str:
    """The pure-Python wheel of `distribution` in the release (of `version` when given)."""
    stem = distribution.replace("-", "_")
    for name in sorted(listed):
        match = re.fullmatch(rf"{re.escape(stem)}-([^-]+)-py3-none-any\.whl", name)
        if match and (version is None or match.group(1) == version):
            return name
    wanted = f"{distribution} {version}" if version else distribution
    raise ReleaseError(f"the release has no wheel for {wanted}")


def fetch(
    source: str,
    names: list[str],
    destination: Path,
    listed: dict[str, str],
    progress: Callable[[str], None] = lambda _line: None,
) -> list[Path]:
    """Copy or download `names` into `destination`, each verified against `SHA256SUMS`."""
    destination.mkdir(parents=True, exist_ok=True)
    paths = []
    for name in names:
        progress(f"fetching {name}")
        data = _read(source, name)
        digest = hashlib.sha256(data).hexdigest()
        if digest != listed[name]:
            raise ReleaseError(
                f"{name} does not match the release's {_SUMS} (got {digest[:12]}..., "
                f"expected {listed[name][:12]}...); not installed"
            )
        path = destination / name
        path.write_bytes(data)
        paths.append(path)
    return paths
