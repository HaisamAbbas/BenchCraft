"""Per-user BenchCraft settings (`~/.benchcraft/config.json`, or `BENCHCRAFT_HOME`): the
assistant model chosen once at first run (`benchcraft setup`), used by every project that
does not name its own with `--provider-config`.

The file holds the endpoint, the model and the *name* of the environment variable with the
API key, never the key. Setup can save the key as a user environment variable (Windows:
HKCU\\Environment, the same place System Properties writes); elsewhere it prints the line
to add to the shell profile.
"""

from __future__ import annotations

import getpass
import json
import os
import sys
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from aibench.planning.openai_provider import OpenAICompatibleConfig

HOME_ENV = "BENCHCRAFT_HOME"


@dataclass(frozen=True)
class Preset:
    label: str
    base_url: str
    model: str | None  # the model used; None: the user types one
    key_env: str


PRESETS: dict[str, Preset] = {
    # GLM-4.7-Flash is free on Z.ai's API, but Z.ai still needs a (free) key; glm-4.6 is paid.
    "1": Preset(
        "Z.ai GLM-4.7-Flash (free model; needs a free API key from z.ai)",
        "https://api.z.ai/api/paas/v4",
        "glm-4.7-flash",
        "ZAI_API_KEY",
    ),
    "2": Preset("OpenAI", "https://api.openai.com/v1", None, "OPENAI_API_KEY"),
    "3": Preset("OpenRouter", "https://openrouter.ai/api/v1", None, "OPENROUTER_API_KEY"),
}


def home() -> Path:
    return Path(os.environ.get(HOME_ENV) or Path.home() / ".benchcraft")


def config_file() -> Path:
    return home() / "config.json"


def load() -> dict[str, Any]:
    try:
        data = json.loads(config_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def decided() -> bool:
    """Whether first-run setup has happened (a model chosen, or explicitly skipped)."""
    return "provider" in load()


def saved_provider() -> OpenAICompatibleConfig | None:
    raw = load().get("provider")
    if not isinstance(raw, dict):
        return None
    try:
        return OpenAICompatibleConfig.model_validate(raw)
    except ValidationError:
        return None


def save_provider(config: OpenAICompatibleConfig | None) -> Path:
    data = load()
    data["provider"] = config.model_dump(mode="json", exclude_none=True) if config else None
    path = config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


KEY_SUFFIXES = ("_API_KEY", "_TOKEN")
NO_USER_ENV = "BENCHCRAFT_NO_USER_ENV"  # set to anything to turn adoption off


def read_user_environment() -> dict[str, str]:
    """The user environment variables Windows has stored for this account (not this
    terminal's copy of them). Empty off Windows."""
    if sys.platform != "win32":
        return {}
    import winreg

    found: dict[str, str] = {}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            index = 0
            while True:
                try:
                    name, value, _kind = winreg.EnumValue(key, index)
                except OSError:
                    break
                index += 1
                if isinstance(value, str) and value:
                    found[name] = value
    except OSError:
        return {}
    return found


def adopt_user_environment(
    reader: Callable[[], dict[str, str]] = read_user_environment,
    environ: MutableMapping[str, str] | None = None,
) -> list[str]:
    """Make the stored API keys and tokens of this account visible to this process.

    A terminal opened before a key was stored never sees it (Windows gives a program its
    environment when it starts), which looked like "secret env:X is not set" in a session
    where the key had just been saved. Only names ending in `_API_KEY` or `_TOKEN` are
    adopted, only when the terminal does not already have them, so a variable a user set
    for this terminal always wins. Returns the names adopted."""
    target = os.environ if environ is None else environ
    if target.get(NO_USER_ENV):
        return []
    adopted = []
    for name, value in reader().items():
        if name.upper().endswith(KEY_SUFFIXES) and not target.get(name):
            target[name] = value
            adopted.append(name)
    return adopted


def persist_user_env(name: str, value: str) -> bool:
    """Save `name` as a user environment variable for new terminals (Windows only), and set
    it for this process. False where BenchCraft cannot do it for the user."""
    os.environ[name] = value
    if sys.platform != "win32":
        return False
    import ctypes
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
    # Tell running programs (Explorer, new terminals) the environment changed.
    result = ctypes.c_ulong()
    ctypes.windll.user32.SendMessageTimeoutW(
        0xFFFF, 0x001A, 0, "Environment", 2, 2000, ctypes.byref(result)
    )
    return True


Ask = Callable[[str], str]


def run_setup(
    say: Callable[[str], None],
    ask: Ask = input,
    secret: Ask = getpass.getpass,
    persist: Callable[[str, str], bool] = persist_user_env,
) -> OpenAICompatibleConfig | None:
    """Interactive first-run setup: choose the assistant's model and where its key is.
    Returns the saved config, or None when the user skips (commands still work)."""
    ask = _without_bom(ask)
    say("BenchCraft's assistant needs a model (an OpenAI-compatible API). Choose one:")
    for choice, preset in PRESETS.items():
        say(f"  [{choice}] {preset.label}")
    say("  [4] another OpenAI-compatible endpoint")
    say("  [s] skip for now (slash commands work without a model)")
    while True:
        choice = ask("Choice [1]: ").strip().lower() or "1"
        if choice in (*PRESETS, "4", "s"):
            break
        say("Type 1, 2, 3, 4 or s.")
    if choice == "s":
        save_provider(None)
        say("Skipped. Run `benchcraft setup` any time to choose a model.")
        return None
    if choice == "4":
        base_url = _ask_required(ask, "Endpoint base URL (https://.../v1): ")
        model = _ask_required(ask, "Model name: ")
        key_env = ask("Environment variable with its API key [BENCHCRAFT_API_KEY]: ").strip()
        key_env = key_env or "BENCHCRAFT_API_KEY"
    else:
        preset = PRESETS[choice]
        base_url = preset.base_url
        # A preset with a known model does not ask (option 4 takes any model).
        model = preset.model or _ask_required(ask, "Model name: ")
        key_env = preset.key_env
    if not os.environ.get(key_env):
        key = secret(f"API key (saved as the user environment variable {key_env}; hidden): ")
        if key.strip():
            if persist(key_env, key.strip()):
                say(f"Saved {key_env} for your user account; new terminals will have it.")
            else:
                say(
                    f"Set for this session. To keep it, add to your shell profile:\n"
                    f"  export {key_env}='<your key>'"
                )
        else:
            say(f"No key entered: set {key_env} before chatting with the assistant.")
    config = OpenAICompatibleConfig(base_url=base_url, model=model, api_key=f"env:{key_env}")
    path = save_provider(config)
    say(f"Assistant model: {model} at {base_url} (saved in {path}; the key is not).")
    return config


def _without_bom(ask: Ask) -> Ask:
    """Piped input (PowerShell's especially) can start with a byte-order mark."""
    return lambda prompt: ask(prompt).lstrip("﻿")


def _ask_required(ask: Ask, prompt: str) -> str:
    while True:
        value = ask(prompt).strip()
        if value:
            return value
