"""The conversation as shown in the terminal: the user's messages as tinted bands, and the
assistant's Markdown as styled text rather than raw asterisks.

Replies arrive as a stream, so `ReplyFormatter` works on one logical line at a time: it
reads the line's block form (heading, bullet, numbered item, quote, rule, code fence),
styles its inline spans, then wraps it to the console width by visible characters, with
continuation lines indented under the text. A line is shown once it completes; only a very
long one prints while still streaming, cut outside any open `**bold**` or `` `code` `` span,
and the unprinted remainder is handed back to continue the same line.

Everything is built as Rich `Text`, never markup, so brackets in a reply print as they are;
secrets are redacted and control sequences removed first, as for any untrusted text.
"""

from __future__ import annotations

import re

from rich.padding import Padding
from rich.text import Text

from aibench.security.redaction import sanitize
from aibench.tui.themes import Theme

_INLINE = re.compile(
    r"\*\*(?P<bold>[^*\n]+?)\*\*"
    r"|__(?P<bold2>[^_\n]+?)__"
    r"|`(?P<code>[^`\n]+)`"
    r"|(?<![*\w])\*(?P<italic>[^*\s](?:[^*\n]*[^*\s])?)\*(?![*\w])"
)
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_NUMBERED = re.compile(r"^(\s*)(\d{1,3})[.)]\s+(.*)$")
_QUOTE = re.compile(r"^\s*>\s?(.*)$")
_RULE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")

_MIN_TEXT_WIDTH = 20
_STREAM_AFTER_WIDTHS = 4  # a line still streaming prints early only past this many widths


class ReplyFormatter:
    def __init__(self, theme: Theme, *, unicode: bool) -> None:
        self.theme = theme
        self.bullet = "•" if unicode else "-"
        self.bar = "│" if unicode else "|"
        self.rule = "─" if unicode else "-"
        self._code = False
        self._continuing = False
        self._hang = Text()  # prefix for the continuation lines of the current line
        self._style = ""  # whole-line style of the current line (headings)

    def feed(self, raw: str, width: int, *, complete: bool) -> tuple[list[Text], str]:
        """Physical lines ready to print for `raw`, and the part still waiting for more of
        the same line (always "" when `complete`)."""
        if self._code:
            return self._code_line(raw, complete)
        if not complete and len(raw) <= _STREAM_AFTER_WIDTHS * width:
            return [], raw  # shown once the line completes
        if not self._continuing:
            if _FENCE.match(raw) or _RULE.match(raw):
                if not complete:
                    return [], raw  # wait: the whole line decides what it is
                if _RULE.match(raw):
                    return [Text(self.rule * min(width - 1, 40), style=self.theme.dim)], ""
                self._code = True
                return [], ""
            prefix, body = self._block(raw)
        else:
            prefix, body = self._hang.copy(), raw
        available = max(width - prefix.cell_len - 1, _MIN_TEXT_WIDTH)
        if complete:
            self._continuing = False
            return self._wrap(prefix, self._inline(body), available), ""
        # A very long line still streaming: print its head, cut outside any open span.
        lines: list[Text] = []
        while len(body) > available:
            cut = _safe_cut(body, available)
            if cut is None:
                break
            lines.append(prefix + self._inline(body[:cut].rstrip()))
            body = body[cut:].lstrip()
            prefix = self._hang.copy()
        if not lines:
            if not self._continuing:
                self._reset_line()
            return [], raw
        self._continuing = True
        return lines, body

    def finish(self) -> None:
        """End the current line (a tool call or the end of the reply interrupts it)."""
        self._continuing = False
        self._reset_line()

    def _code_line(self, raw: str, complete: bool) -> tuple[list[Text], str]:
        if not complete:
            return [], raw
        if _FENCE.match(raw):
            self._code = False
            return [], ""
        return [Text("  " + sanitize(raw), style=self.theme.accent)], ""

    def _block(self, raw: str) -> tuple[Text, str]:
        """The line's leading prefix and remaining text; sets the hanging indent and style
        for its continuation lines."""
        accent, dim = self.theme.accent, self.theme.dim
        self._reset_line()
        if match := _HEADING.match(raw):
            self._style = f"bold {self.theme.title}"
            return Text(), match.group(1)
        if match := _BULLET.match(raw):
            prefix = Text(f"{match.group(1)}{self.bullet} ", style=accent)
            self._hang = Text(" " * prefix.cell_len)
            return prefix, match.group(2)
        if match := _NUMBERED.match(raw):
            prefix = Text(f"{match.group(1)}{match.group(2)}. ", style=accent)
            self._hang = Text(" " * prefix.cell_len)
            return prefix, match.group(3)
        if match := _QUOTE.match(raw):
            self._hang = Text(f"{self.bar} ", style=dim)
            self._style = f"italic {dim}"
            return self._hang.copy(), match.group(1)
        return Text(), raw

    def _wrap(self, prefix: Text, text: Text, available: int) -> list[Text]:
        """`text` broken at spaces into lines of at most `available` visible characters
        (styles carried across), the first after `prefix`, the rest after the hang."""
        plain, pieces, start = text.plain, [], 0
        while len(plain) - start > available:
            cut = plain.rfind(" ", start, start + available + 1)
            skip = 1
            if cut <= start:  # one word wider than the line: break it
                cut, skip = start + available, 0
            pieces.append(text[start:cut])
            start = cut + skip
        pieces.append(text[start:])
        return [(prefix if i == 0 else self._hang.copy()) + p for i, p in enumerate(pieces)]

    def _reset_line(self) -> None:
        self._hang = Text()
        self._style = ""

    def _inline(self, text: str) -> Text:
        text = sanitize(text)
        out = Text(style=self._style)
        position = 0
        for match in _INLINE.finditer(text):
            out.append(text[position : match.start()])
            if match.group("bold") or match.group("bold2"):
                bold = match.group("bold") or match.group("bold2")
                out.append(bold, style=f"bold {self.theme.title}")
            elif match.group("code"):
                out.append(match.group("code"), style=self.theme.accent)
            else:
                out.append(match.group("italic"), style="italic")
            position = match.end()
        out.append(text[position:])
        return out


def user_band(text: str, theme: Theme, *, unicode: bool) -> Padding:
    """The user's sent message, as a full-width tinted band with a marker."""
    marker = "›" if unicode else ">"
    body = Text()
    for index, line in enumerate(text.split("\n")):
        if index:
            body.append("\n")
        body.append(f"{marker} " if index == 0 else "  ", style=f"bold {theme.accent}")
        body.append(sanitize(line), style=theme.text)
    return Padding(body, (1, 2), style=f"on {theme.user_bg}", expand=True)


def _safe_cut(body: str, limit: int) -> int | None:
    """The last space at or before `limit` that is outside any bold or code span."""
    cut = body.rfind(" ", 0, limit + 1)
    while cut > 0:
        head = body[:cut]
        if head.count("**") % 2 == 0 and head.count("`") % 2 == 0:
            return cut
        cut = body.rfind(" ", 0, cut)
    return None
