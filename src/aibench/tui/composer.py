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
from prompt_toolkit.layout import walk
from prompt_toolkit.layout.containers import (
    ConditionalContainer,
    FloatContainer,
    HSplit,
    VSplit,
    Window,
)
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.processors import Processor, Transformation, TransformationInput

from aibench.tui.themes import Theme

PLACEHOLDER = "Ask BenchCraft to do anything"  # what the box says while it is empty

_LAST_COLUMN = 1  # the buffer's own trailing space; see the module docstring
_FILLER_WEIGHT = 1_000  # takes nearly all spare rows, so the input's own window takes none
_MENU_ROWS = 10  # rows kept for the completion menu while it is open
_GAP_ROWS = 1  # always a blank row between the conversation (or the banner) and the box
_CHROME_ROWS = 8  # the Working line, the box, the gap and the status bar, with a margin


def _pad_row() -> VSplit:
    """A row of the box with no text: its edge, then the band out to where the input line's
    band ends (one column short of the right edge, as the input line is)."""
    return VSplit(
        [
            Window(width=1, height=1, char="▌", style="class:composer-bar"),
            Window(height=1, char=" ", style="class:composer-band"),
            Window(width=_LAST_COLUMN, height=1),
        ]
    )


def pin_to_bottom(session: PromptSession[str], box: ComposerBand | None = None) -> None:
    """Keep the box on the last rows of the terminal, just above the status bar; with `box`
    (and glyphs the terminal can print), give it a padding row above and below the input and
    a blank row before the status bar, so the box stands clear of what surrounds it.

    Outside full-screen mode prompt_toolkit fills every row from the cursor to the bottom of
    the screen (that is what keeps the status bar on the last row), and gives the spare rows to
    the input's window, which leaves them empty *under* the box. An empty row that stretches,
    put first, takes those rows instead, so they sit *above* the box: the Working line, the box
    and the status bar stay together at the bottom while replies print above.

    The filler goes inside the input's float container, not above it: the completion menu
    floats within that container, so the spare rows above the box are where the menu for `/`
    opens. The filler is dropped once the input is accepted, so the finished prompt takes no
    extra rows.
    """
    root = session.app.layout.container
    main = root.children[0] if isinstance(root, HSplit) and root.children else None
    floats = getattr(main, "alternative_content", None)  # the unframed input container
    inner = getattr(floats, "content", None)
    if not isinstance(floats, FloatContainer) or not isinstance(inner, HSplit):
        return  # an unexpected layout: keep prompt_toolkit's own
    open_ = Condition(lambda: not session.app.is_done)

    def filler_height() -> Dimension:
        # While completions are listed, the rows above the box are where the menu opens, so
        # claim enough of them; on a short terminal prompt_toolkit then scrolls to make room.
        # Never more than the rows left beside the box and the bars: a layout taller than the
        # terminal makes prompt_toolkit blank the prompt ("Window too small").
        state = session.default_buffer.complete_state
        if not (state and state.completions):
            return Dimension(min=_GAP_ROWS, weight=_FILLER_WEIGHT)
        free = max(session.app.output.get_size().rows - _CHROME_ROWS, 0)
        rows = min(len(state.completions), _MENU_ROWS, free)
        return Dimension(min=max(rows, _GAP_ROWS), weight=_FILLER_WEIGHT)

    filler = Window(height=filler_height, always_hide_cursor=True)
    inner.children.insert(0, ConditionalContainer(filler, filter=open_))
    if box is None or not box.unicode:
        return
    input_window = session.app.layout.current_window
    row = next((i for i, child in enumerate(inner.children) if input_window in walk(child)), None)
    if row is None:
        return
    inner.children[row + 1 : row + 1] = [
        ConditionalContainer(_pad_row(), filter=open_),
        ConditionalContainer(Window(height=1), filter=open_),  # clear of the status bar
    ]
    inner.children.insert(row, ConditionalContainer(_pad_row(), filter=open_))


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
