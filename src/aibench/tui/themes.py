"""Colour themes for the interactive terminal's chrome: the welcome banner, the input box, the
bottom toolbar and the completion menu. Status colours inside cards (pass/fail/warning) stay
semantic and do not change with the theme.

The choice is saved per project in the workspace (`/themes NAME`); a missing, unreadable or
unknown saved value falls back to the default without failing the session.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from prompt_toolkit.styles import Style


@dataclass(frozen=True)
class Theme:
    name: str
    description: str
    logo: tuple[str, str, str, str, str, str]  # one colour per logo row, top to bottom
    border: str
    title: str
    accent: str
    dim: str
    text: str
    toolbar_bg: str
    toolbar_fg: str
    menu_bg: str
    menu_selected_bg: str
    user_bg: str  # band behind the user's own messages
    composer_bg: str  # band behind the input being typed

    def prompt_style(self) -> Style:
        return Style.from_dict(
            {
                "prompt": f"bold {self.accent}",
                "bottom-toolbar": f"noreverse bg:{self.toolbar_bg} {self.toolbar_fg}",
                "completion-menu.completion": f"bg:{self.menu_bg} {self.text}",
                "completion-menu.completion.current": f"bg:{self.menu_selected_bg} {self.title}",
                "completion-menu.meta.completion": f"bg:{self.menu_bg} {self.dim}",
                "completion-menu.meta.completion.current": f"bg:{self.menu_selected_bg} {self.text}",
                "working": self.dim,
                "working.spinner": f"bold {self.accent}",
                "working.key": f"bold {self.text}",
                "composer-band": f"noreverse bg:{self.composer_bg}",
                "composer-gutter": f"bold {self.accent}",
                "composer-bar": f"bold {self.border}",
                "composer-placeholder": f"noreverse bg:{self.composer_bg} {self.dim}",
                "prompt-continuation": "noreverse",
            }
        )


DEFAULT_THEME = "crimson"

THEMES: dict[str, Theme] = {
    theme.name: theme
    for theme in (
        Theme(
            "crimson",
            "BenchCraft red (default)",
            ("#FF4D4D", "#F03A3A", "#DC2626", "#C21F1F", "#A31B1B", "#7F1D1D"),
            border="#B91C1C",
            title="#FF5C5C",
            accent="#EF4444",
            dim="#A05252",
            text="#F7E4E4",
            toolbar_bg="#2A0E0E",
            toolbar_fg="#F1D0D0",
            menu_bg="#2A0E0E",
            menu_selected_bg="#5C1A1A",
            user_bg="#4A2629",
            composer_bg="#2A2124",
        ),
        Theme(
            "ember",
            "burnt orange and coal",
            ("#FFB347", "#FF9A3C", "#F97F2A", "#E8651F", "#C94E17", "#9A3A12"),
            border="#C2410C",
            title="#FFB347",
            accent="#F97316",
            dim="#A0643E",
            text="#FCE9D8",
            toolbar_bg="#261409",
            toolbar_fg="#F5D6BC",
            menu_bg="#261409",
            menu_selected_bg="#5A2C12",
            user_bg="#46301F",
            composer_bg="#2A2320",
        ),
        Theme(
            "gold",
            "classic gold and bronze",
            ("#FFD700", "#FFD700", "#FFBF00", "#FFBF00", "#CD7F32", "#CD7F32"),
            border="#CD7F32",
            title="#FFD700",
            accent="#FFBF00",
            dim="#B8860B",
            text="#FFF8DC",
            toolbar_bg="#1A1A2E",
            toolbar_fg="#C0C0C0",
            menu_bg="#1A1A2E",
            menu_selected_bg="#333355",
            user_bg="#3D3522",
            composer_bg="#26262E",
        ),
        Theme(
            "ocean",
            "deep blue and seafoam",
            ("#7DD3FC", "#5BC0F0", "#3AA6E0", "#2B88C8", "#2168A8", "#1B4F85"),
            border="#2563EB",
            title="#7DD3FC",
            accent="#38BDF8",
            dim="#5A7896",
            text="#DDEBF7",
            toolbar_bg="#0E1A2E",
            toolbar_fg="#C9DBEE",
            menu_bg="#0E1A2E",
            menu_selected_bg="#1E3A5F",
            user_bg="#1F3148",
            composer_bg="#1E242E",
        ),
        Theme(
            "forest",
            "pine green and moss",
            ("#86EFAC", "#6EE094", "#4ADE80", "#34C26A", "#239F55", "#1A7A42"),
            border="#15803D",
            title="#86EFAC",
            accent="#4ADE80",
            dim="#5E8A6C",
            text="#E0F5E6",
            toolbar_bg="#0D1F14",
            toolbar_fg="#C8E6D1",
            menu_bg="#0D1F14",
            menu_selected_bg="#1D4A2E",
            user_bg="#1E3A28",
            composer_bg="#1E2620",
        ),
        Theme(
            "violet",
            "violet and magenta",
            ("#E9A8FF", "#D98CFA", "#C471F5", "#A855F7", "#8B3FD9", "#6D28B8"),
            border="#7E22CE",
            title="#E9A8FF",
            accent="#C084FC",
            dim="#7F6A96",
            text="#F1E6FA",
            toolbar_bg="#1C1029",
            toolbar_fg="#DCCBEB",
            menu_bg="#1C1029",
            menu_selected_bg="#3E2360",
            user_bg="#352545",
            composer_bg="#241E2A",
        ),
        Theme(
            "mono",
            "clean grayscale",
            ("#F0F0F0", "#D6D6D6", "#BDBDBD", "#A3A3A3", "#8A8A8A", "#707070"),
            border="#5E5E5E",
            title="#E6EDF3",
            accent="#AAAAAA",
            dim="#707070",
            text="#C9D1D9",
            toolbar_bg="#1F1F1F",
            toolbar_fg="#C9D1D9",
            menu_bg="#1F1F1F",
            menu_selected_bg="#464646",
            user_bg="#3A3A3A",
            composer_bg="#262626",
        ),
        Theme(
            "paper",
            "dark red ink for light terminal backgrounds",
            ("#B91C1C", "#AA1A1A", "#9B1818", "#8C1616", "#7D1414", "#6E1212"),
            border="#B91C1C",
            title="#991B1B",
            accent="#B91C1C",
            dim="#6B5B5B",
            text="#1F1414",
            toolbar_bg="#F6E7E7",
            toolbar_fg="#3B1F1F",
            menu_bg="#FBF3F3",
            menu_selected_bg="#F2CFCF",
            user_bg="#F7DCDC",
            composer_bg="#EFE9E9",
        ),
    )
}


def get_theme(name: str | None) -> Theme:
    """The named theme, or the default for an unknown or missing name."""
    return THEMES.get((name or "").strip().lower(), THEMES[DEFAULT_THEME])


def load_theme(path: Path | None) -> Theme:
    """The saved theme, or the default when there is none or it cannot be read."""
    if path is None:
        return get_theme(None)
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return get_theme(None)
    return get_theme(saved.get("theme") if isinstance(saved, dict) else None)


def save_theme(path: Path, theme: Theme) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"theme": theme.name}) + "\n", encoding="utf-8")
