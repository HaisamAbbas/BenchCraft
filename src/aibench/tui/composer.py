"""The chat input box at the bottom of the terminal (§13): a band across the full width with a
coloured marker, so it is never in doubt where typing goes while the conversation scrolls above.

The box is drawn by the prompt itself, never printed. prompt_toolkit repeats the prompt's last line
as the prefix of every input line, so the gutter comes from the prompt message and the same gutter
again from the continuation; one input processor then tints the line and fills the band to the
right edge. The fill stops one column short, because the buffer always appends a space of its own
and padding over that space would wrap the line onto a second row.

Only the editable line is boxed. A sent message keeps its own tint (`tui.reply.user_band`) and the
replies stay on the terminal's background, so the box frames the input without competing with what
is being said. When the terminal cannot print the marker glyphs the band collapses to a plain marker
on unbanded text, which keeps a legacy Windows code page usable.
"""

from __future__ import annotations

from prompt_toolkit import PromptSession
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.formatted_text.utils import fragment_list_width
from prompt_toolkit.layout.containers import ConditionalContainer, HSplit, Window
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.processors import Processor, Transformation, TransformationInput

from aibench.tui.themes import Theme

PLACEHOLDER = "Ask BenchCraft to do anything"  # what the box says while it is empty

_LAST_COLUMN = 1  # the buffer's own trailing space; see the module docstring
_FILLER_WEIGHT = 1_000  # takes nearly all spare rows, so the input's own window takes none


def pin_to_bottom(session: PromptSession[str]) -> None:
    """Keep the box on the last rows of the terminal, just above the status bar.

    Outside full-screen mode prompt_toolkit fills every row from the cursor to the bottom of
    the screen (that is what keeps the status bar on the last row), and gives the spare rows to
    the input's window, which leaves them empty *under* the box. An empty row that stretches,
    put first, takes those rows instead, so they sit *above* the box: the Working line, the box
    and the status bar stay together at the bottom while replies print above. The filler is
    dropped once the input is accepted, so the finished prompt takes no extra rows.
    """
    root = session.app.layout.container
    if not isinstance(root, HSplit):
        return
    filler = Window(height=Dimension(min=0, weight=_FILLER_WEIGHT), always_hide_cursor=True)
    root.children.insert(
        0, ConditionalContainer(filler, filter=Condition(lambda: not session.app.is_done))
    )


class ComposerBand(Processor):
    """The input box: the tint behind the text, the fill to the right edge, the placeholder.

    One instance for the life of the prompt. The prompt uses `gutter` as the prefix of the first
    input line and `continuation` for every line after it, wrapped or typed, so all of them share
    one left edge, and `set_theme` repaints that same instance when `/themes` switches.
    """

    def __init__(self, theme: Theme, *, unicode: bool, suggestion: str | None = None) -> None:
        # Set by the app when something else already shows in the placeholder's place; a
        # suggestion from the history is the only thing that ever does.
        self.suggestion = suggestion
        self.theme = theme
        self.unicode = unicode
        self.gutter: StyleAndTextTuples = []
        self.width = 0
        self._paint()

    def set_theme(self, theme: Theme) -> None:
        """Take on another theme's colours, in place, so the open box follows `/themes`."""
        self.theme = theme
        self._paint()

    def _paint(self) -> None:
        self.gutter = (
            [
                ("class:composer-bar", "▌"),
                ("class:composer-band", " "),
                ("class:composer-gutter", "❯"),
                ("class:composer-band", " "),
            ]
            if self.unicode
            else [("class:composer-gutter", "> ")]
        )
        self.width = fragment_list_width(self.gutter)

    def continuation(self, width: int, line_number: int, wrap_count: int) -> StyleAndTextTuples:
        """`prompt_continuation`: the same gutter before every further line of the input."""
        return self.gutter

    def apply_transformation(self, transformation_input: TransformationInput) -> Transformation:
        """Tint what is on the line, then fill the band out to the right edge."""
        tint = f"class:composer-band {self.theme.text}" if self.unicode else self.theme.text
        fragments: StyleAndTextTuples = [
            (f"{tint} {style}", text) for style, text, *_ in transformation_input.fragments
        ]
        if self._placeholder_due(transformation_input):
            fragments = [*fragments, ("class:composer-placeholder", PLACEHOLDER)]
        if self.unicode:
            fill = transformation_input.width - self.width - _LAST_COLUMN
            padding = max(fill - fragment_list_width(fragments), 0)
            if padding:
                fragments = [*fragments, ("class:composer-band", " " * padding)]
        return Transformation(fragments)

    def _placeholder_due(self, transformation_input: TransformationInput) -> bool:
        """An empty input on its last line, with nothing else shown in the placeholder's place."""
        buffer = transformation_input.buffer_control.buffer
        return (
            buffer.text == ""
            and transformation_input.lineno == transformation_input.document.line_count - 1
            and not (self.suggestion or buffer.suggestion)
        )
