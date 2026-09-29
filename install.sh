#!/bin/sh
# BenchCraft installer for macOS and Linux:
#
#   curl -fsSL https://raw.githubusercontent.com/HaisamAbbas/BenchCraft/main/install.sh | sh
#
# Installs uv if missing (https://astral.sh/uv; it brings its own Python), downloads the
# BenchCraft wheel from a GitHub Release, verifies it against the release's SHA256SUMS and
# installs it as an isolated tool: `benchcraft` on PATH, nothing in your projects.
#
# Optional environment variables:
#   BENCHCRAFT_VERSION   a release tag, e.g. v0.1.0rc1 (default: the newest release)
#   BENCHCRAFT_RELEASES  a folder or URL holding a release's files (for testing a build)
#   BENCHCRAFT_NO_MODIFY_PATH=1  leave PATH alone (uv's tool bin directory is not added)
set -eu

REPO="HaisamAbbas/BenchCraft"
say() { printf 'benchcraft: %s\n' "$1"; }
fail() { say "error: $1" >&2; exit 1; }

if ! command -v uv >/dev/null 2>&1; then
    say "installing uv (the Python tool manager from https://astral.sh/uv)"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    PATH="$HOME/.local/bin:$PATH"
    command -v uv >/dev/null 2>&1 || fail "uv was installed but is not on PATH; open a new terminal and run this again"
fi

SOURCE="${BENCHCRAFT_RELEASES:-}"
if [ -z "$SOURCE" ]; then
    TAG="${BENCHCRAFT_VERSION:-}"
    if [ -z "$TAG" ]; then
        # The most recently published release, pre-releases included (releases/latest skips
        # those). Not the first one listed: the list is not in publish order (rc10 came
        # below rc5), so each tag is paired with its publish time and the newest is taken.
        TAG=$(curl -fsSL "https://api.github.com/repos/$REPO/releases?per_page=100" \
            | grep -o '"tag_name": *"[^"]*"\|"published_at": *"[^"]*"' \
            | sed 's/.*: *"\([^"]*\)"/\1/' | paste -d ' ' - - | sort -k2 -r \
            | head -n 1 | cut -d ' ' -f 1)
        [ -n "$TAG" ] || fail "no BenchCraft release found at github.com/$REPO"
    fi
    SOURCE="https://github.com/$REPO/releases/download/$TAG"
fi
say "installing from $SOURCE"

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
fetch() {
    case "$SOURCE" in
        http://*|https://*) curl -fsSL "$SOURCE/$1" -o "$WORK/$1" ;;
        *) cp "$SOURCE/$1" "$WORK/$1" ;;
    esac
}

fetch SHA256SUMS
LINE=$(grep -E '^[0-9a-f]{64} +\*?aibench-[^-]+-py3-none-any\.whl$' "$WORK/SHA256SUMS" | head -n 1 || true)
[ -n "$LINE" ] || fail "the release at $SOURCE lists no BenchCraft wheel"
EXPECTED=$(printf '%s' "$LINE" | awk '{print $1}')
WHEEL=$(printf '%s' "$LINE" | awk '{print $2}' | sed 's/^\*//')

fetch "$WHEEL"
if command -v sha256sum >/dev/null 2>&1; then
    ACTUAL=$(sha256sum "$WORK/$WHEEL" | awk '{print $1}')
else
    ACTUAL=$(shasum -a 256 "$WORK/$WHEEL" | awk '{print $1}')
fi
[ "$ACTUAL" = "$EXPECTED" ] || fail "$WHEEL does not match the release's SHA256SUMS; not installed"

say "installing $WHEEL"
uv tool install --force --python 3.12 "$WORK/$WHEEL"
[ -n "${BENCHCRAFT_NO_MODIFY_PATH:-}" ] || uv tool update-shell >/dev/null 2>&1 || true

say "done. Open a new terminal, go to your project and type: benchcraft"
say "update: run this installer again; remove: uv tool uninstall aibench"
