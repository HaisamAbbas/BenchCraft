"""The chat input box: a tinted band with a marker, its placeholder, and the ASCII fallback.

These are the parts of the box that can be checked without a terminal: what the gutter looks
like, how the band is filled to the right edge, and when the placeholder is shown. How it is
finally drawn is left to the interactive terminal, which has its own real-console test.
"""

from __future__ import annotations

from prompt_toolkit.auto_suggest import Suggestion
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.layout.controls import BufferControl
from prompt_toolkit.layout.processors import TransformationInput

from aibench.tui import composer
from aibench.tui.themes import THEMES, Theme, get_theme

WIDTH = 60  # the terminal width the band is filled for
AVAILABLE = WIDTH - 4 - 1  # the gutter and the buffer's own trailing space


def _box(theme: Theme | None = None) -> composer.ComposerBand:
    return composer.ComposerBand(theme or get_theme(None), unicode=True)


def _line(box: composer.ComposerBand, text: str = "", *, width: int = WIDTH) -> StyleAndTextTuples:
    """What the box makes of one line of input, as prompt_toolkit would hand it over."""
    buffer = Buffer()
    buffer.text = text
    if box.suggestion is not None:  # only a test needs this; the app never suggests
        buffer.suggestion = Suggestion(box.suggestion)
    transformation = TransformationInput(
        buffer_control=BufferControl(buffer=buffer),
        document=buffer.document,
        lineno=0,
        source_to_display=lambda i: i,
        fragments=[("class:input", text)],
        width=width,
        height=1,
    )
    return box.apply_transformation(transformation).fragments


def _text(fragments: StyleAndTextTuples) -> str:
    return "".join(text for _, text, *_ in fragments)


def _styles(fragments: StyleAndTextTuples) -> str:
    return " ".join(style for style, _, *_ in fragments)


def test_gutter_marks_the_left_edge_of_the_band() -> None:
    box = _box()
    assert _text(box.gutter) == "▌ ❯ "
    assert box.width == 4
    styles = _styles(box.gutter)
    assert "class:composer-bar" in styles and "class:composer-band" in styles
    # Every further line of the input starts with the same gutter, so wrapping keeps the edge.
    assert box.continuation(0, 1, 0) is box.gutter


def test_the_band_is_tinted_and_filled_to_the_right_edge() -> None:
    theme = get_theme(None)
    box = _box(theme)
    fragments = _line(box, "run the benchmark")
    # What is typed keeps the theme's colour, and it sits on the band's tint.
    assert fragments[0] == (f"class:composer-band {theme.text} class:input", "run the benchmark")
    assert fragments[-1] == ("class:composer-band", " " * (AVAILABLE - 17))
    assert _text(fragments) == "run the benchmark" + " " * (AVAILABLE - 17)
    # The fill never reaches the last column: that one is left to the buffer's own trailing
    # space, and padding over it would wrap the line onto a second row.
    for length in range(0, WIDTH * 2, 7):
        assert len(_text(_line(box, "x" * length))) == max(length, AVAILABLE), length


def test_the_placeholder_shows_only_on_an_empty_input() -> None:
    box = _box()
    shown = _line(box)
    assert _text(shown).startswith(composer.PLACEHOLDER)
    assert "class:composer-placeholder" in _styles(shown)
    assert composer.PLACEHOLDER not in _text(_line(box, "/status"))


def test_a_suggestion_replaces_the_placeholder() -> None:
    """A suggestion is shown in the same place, so the two must never be shown together."""
    box = _box()
    box.suggestion = "run the benchmark"
    assert composer.PLACEHOLDER not in _text(_line(box))


def test_ascii_fallback_is_a_plain_marker_with_no_band() -> None:
    box = composer.ComposerBand(get_theme("mono"), unicode=False)
    assert _text(box.gutter) == "> "
    assert _text(_line(box, "hello")) == "hello"
    assert "composer-band" not in _styles(_line(box, "hello"))
    assert composer.PLACEHOLDER in _text(_line(box))  # still offered, and still printable


def test_the_box_prints_on_a_legacy_windows_code_page() -> None:
    """The band needs the block glyph; the marker, the placeholder and the text are ASCII."""
    box = composer.ComposerBand(get_theme("mono"), unicode=False)
    for fragments in (box.gutter, _line(box, "run the benchmark")):
        _text(fragments).encode("cp1252")  # every character printable on the code page
    assert composer.PLACEHOLDER.encode("ascii")


def test_every_theme_paints_the_box_in_its_own_colours() -> None:
    for theme in THEMES.values():
        style = theme.prompt_style()
        band = style.get_attrs_for_style_str("class:composer-band")
        assert band.bgcolor == theme.composer_bg.lstrip("#")
        assert band.reverse is False
        placeholder = style.get_attrs_for_style_str("class:composer-placeholder")
        assert placeholder.bgcolor == band.bgcolor  # the placeholder is on the band, not below it
        gutter = style.get_attrs_for_style_str("class:composer-gutter")
        assert gutter.color == theme.accent.lstrip("#") and gutter.bold
        assert style.get_attrs_for_style_str("class:composer-bar").color == theme.border.lstrip("#")
        # What is typed has to be readable on the band.
        assert theme.text != theme.composer_bg


def test_the_box_is_pinned_above_the_status_bar() -> None:
    """Spare rows go to a filler before the input, not under it; it is gone once accepted."""
    from prompt_toolkit import PromptSession
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.layout.containers import ConditionalContainer
    from prompt_toolkit.output import DummyOutput

    with create_pipe_input() as pipe:
        session: PromptSession[str] = PromptSession(
            input=pipe, output=DummyOutput(), bottom_toolbar="status"
        )
        before = len(session.app.layout.container.children)
        composer.pin_to_bottom(session)
        children = session.app.layout.container.children
        assert len(children) == before + 1
        filler = children[0]
        assert isinstance(filler, ConditionalContainer)
        assert filler.filter() is True  # shown while the prompt is open
        dimension = filler.content.preferred_height(80, 40)
        assert dimension.weight > 1 and dimension.min == 0
