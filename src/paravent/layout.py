"""Rebuilding paragraphs from the pieces of text a page is read as.

The OCR and a PDF's text layer both give a page as pieces of lines, each with
its box. Pieces on the same row are put back together, left to right. A row
then continues the paragraph above it when it follows closely, in the same
size of type, and the row above was full: the first word of this one would
not have fitted at its end. An address, a list or a title keeps its lines
apart; a word cut by a hyphen at the end of a row keeps its hyphen.
"""

from __future__ import annotations

import re
import statistics
from collections.abc import Callable
from dataclasses import dataclass

# Two pieces are on the same row when they share this much of the smaller height.
ROW_OVERLAP = 0.5
# Between two rows of a paragraph, at most this much blank space, in line heights.
LINE_GAP = 0.9
# A larger jump in box height means another size of type (a title). Boxes of the
# same type vary with ascenders and descenders, hence the margin.
SIZE_RATIO = 1.5
_LIST_ITEM = re.compile(r"^(?:[-–—•·*▪◦►]|\d{1,2}[.)°]|[a-zA-Z][.)])\s")
# Pieces of a row this far apart, in characters, are columns: a table's rows are never
# joined, even when the last column reaches the right edge (amounts on a statement).
COLUMN_GAP = 2
# "Numéro allocataire : …", "IBAN : …": a form's field starts its own line.
_FIELD = re.compile(r"^[^\W\d_][^:.]{0,30}:")
_SENTENCE_END = re.compile(r"[.!?…]$")


@dataclass
class Piece:
    text: str
    left: float
    top: float  # y grows downwards
    right: float
    bottom: float
    score: float | None = None  # OCR confidence; None for a text layer

    @property
    def height(self) -> float:
        return self.bottom - self.top


@dataclass
class _Row:
    pieces: list[Piece]

    @property
    def text(self) -> str:
        return " ".join(piece.text for piece in self.pieces)

    @property
    def left(self) -> float:
        return self.pieces[0].left

    @property
    def right(self) -> float:
        return max(piece.right for piece in self.pieces)

    @property
    def top(self) -> float:
        return min(piece.top for piece in self.pieces)

    @property
    def bottom(self) -> float:
        return max(piece.bottom for piece in self.pieces)

    @property
    def height(self) -> float:  # not bottom - top: a skewed scan spreads a row
        return statistics.median(piece.height for piece in self.pieces)

    @property
    def char_width(self) -> float:
        return sum(p.right - p.left for p in self.pieces) / max(sum(len(p.text) for p in self.pieces), 1)

    @property
    def columns(self) -> bool:
        gaps = (b.left - a.right for a, b in zip(self.pieces, self.pieces[1:]))
        return any(gap > COLUMN_GAP * self.char_width for gap in gaps)


def reading_order(pieces: list[Piece]) -> list[Piece]:
    return [piece for row in _rows(pieces) for piece in row.pieces]


def paragraphs(pieces: list[Piece], render: Callable[[Piece, int], str] = lambda piece, number: piece.text) -> str:
    """The page's text, one paragraph per block; ``render`` writes each piece,
    numbered from 1 in reading order (to flag the doubtful ones)."""
    rows = _rows(pieces)
    if not rows:
        return ""
    line_height = statistics.median(row.height for row in rows)
    # Something further right (a date, a page number) only makes rows look shorter:
    # they stay apart, as they would without this rebuilding, rather than merge.
    right_edge = max(row.right for row in rows)
    rendered = {}
    for number, piece in enumerate((piece for row in rows for piece in row.pieces), start=1):
        rendered[id(piece)] = render(piece, number)

    blocks: list[str] = []
    previous = None
    for row in rows:
        text = " ".join(rendered[id(piece)] for piece in row.pieces)
        if previous and _continues(previous, row, right_edge, line_height):
            hyphen = previous.text.endswith("-") and row.text[:1].islower()
            blocks[-1] += ("" if hyphen else " ") + text
        else:
            blocks.append(text)
        previous = row
    return "\n\n".join(blocks)


def _rows(pieces: list[Piece]) -> list[_Row]:
    rows: list[_Row] = []
    for piece in sorted(pieces, key=lambda p: (p.top + p.bottom) / 2):
        if rows and _same_row(rows[-1], piece):
            rows[-1].pieces.append(piece)
        else:
            rows.append(_Row([piece]))
    for row in rows:
        row.pieces.sort(key=lambda p: p.left)
    return rows


def _same_row(row: _Row, piece: Piece) -> bool:
    last = row.pieces[-1]
    overlap = min(last.bottom, piece.bottom) - max(last.top, piece.top)
    return overlap >= ROW_OVERLAP * min(last.height, piece.height)


def _continues(above: _Row, row: _Row, right_edge: float, line_height: float) -> bool:
    if row.top - above.bottom > LINE_GAP * line_height:
        return False
    if not 1 / SIZE_RATIO <= row.height / above.height <= SIZE_RATIO:
        return False
    if above.columns or row.columns:
        return False
    if _LIST_ITEM.match(row.text) or _FIELD.match(row.text) or above.text.endswith(":"):
        return False
    char_width = above.char_width
    room = right_edge - above.right
    # A sentence ended with room to spare: at worst, a paragraph split between two sentences.
    if _SENTENCE_END.search(above.text) and row.text[:1].isupper() and room >= char_width:
        return False
    return room < (len(row.text.split()[0]) + 1) * char_width
