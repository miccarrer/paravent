"""Convert a source document (PDF, image, Word or e-mail) to Markdown.

PDF pages that carry a text layer are read directly; pages without one
(scans) are rendered and go through OCR. The OCR models ship inside the
rapidocr wheel, so nothing is downloaded at runtime.

Word documents and e-mails have no pages: they come out as a single block.
An e-mail's attachments are not converted here; the corpus imports each one
as a document of its own (see ``mail_attachments``).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from functools import cache
from html.parser import HTMLParser
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image, ImageStat

from . import layout

OCR_DPI = 200
# A PDF page with less extractable text than this is treated as a scan.
MIN_TEXT_CHARS = 25
# OCR lines scored below this are flagged in the Markdown, never silently kept.
LOW_CONFIDENCE = 0.80
# RapidOCR drops the lines it scores below 0.5 unless told otherwise: Paravent
# keeps them all (flagged), and only uses this score to call a page blank.
TEXT_SCORE = 0.5
# A fragment this short below TEXT_SCORE is most likely a logo, a stamp or a
# speck (measured on a real corpus: 85 % of what RapidOCR used to drop). It is
# kept, in a comment at the end of the page, out of the text and of the review.
NOISE_MAX_CHARS = 3
# A page where the OCR read nothing is blank (a verso) if, once shrunk to about
# 50 dpi — isolated specks of the scan fade out — and without its margins,
# almost none of it is ink, i.e. clearly darker than the paper. The recto
# showing through stays lighter than that; a small signature or a word in
# pencil leaves at least twice the allowed share (see tests/test_extract.py).
BLANK_WIDTH = 400
BLANK_MARGIN = 0.08  # scanner edges; punch holes reach 16 mm from the edge (ISO 838)
BLANK_MAX_INK = 1e-4
INK_CONTRAST = 40  # grey levels below the paper's median
MIN_PAPER = 150  # a darker page (photo in dim light, coloured paper) is never called blank

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}
SUPPORTED_SUFFIXES = {".pdf", ".docx", ".eml"} | IMAGE_SUFFIXES
UNPAGED = {"docx", "eml"}


class UnsupportedFormat(ValueError):
    pass


@dataclass
class Page:
    number: int
    text: str
    method: str  # "text", "ocr" or "blank" for PDF pages and images, "docx" or "eml" for unpaged sources
    min_confidence: float | None = None
    lines: int | None = None  # pieces the OCR read, flags number them « ligne i/n »
    noise: list[tuple[str, float]] = field(default_factory=list)  # probable noise, out of the text


def convert(path: Path) -> str:
    return to_markdown(extract_pages(path))


def extract_pages(path: Path, on_page: Callable[[int, int], None] | None = None) -> list[Page]:
    """The pages of a document; ``on_page(done, total)`` is called before the first and after each."""
    on_page = on_page or (lambda done, total: None)
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _pdf_pages(path, on_page)
    if suffix in IMAGE_SUFFIXES:
        on_page(0, 1)
        with Image.open(path) as image:
            pages = [_ocr_page(1, image)]
    elif suffix == ".docx":
        pages = [Page(1, _docx_text(path), "docx")]
    elif suffix == ".eml":
        pages = [Page(1, _mail_text(_read_mail(path)), "eml")]
    else:
        raise UnsupportedFormat(f"Format non pris en charge : {path.suffix or path.name}")
    on_page(1, 1)
    return pages


def to_markdown(pages: list[Page]) -> str:
    blocks = []
    for page in pages:
        if page.method in UNPAGED:
            blocks += [f"<!-- {page.method} -->", page.text or "<!-- document vide -->"]
            continue
        header = f"<!-- page {page.number} · {page.method}"
        if page.lines is not None:
            header += f" · {page.lines} lignes"
        if page.noise:
            header += f" · {len(page.noise)} fragments"
        if page.min_confidence is not None:
            header += f" · confiance min {page.min_confidence:.2f}"
        blocks.append(header + " -->")
        empty = "<!-- page blanche -->" if page.method == "blank" else "<!-- page vide ou illisible -->"
        blocks.append(page.text or empty)
        if page.noise:
            fragments = " · ".join(f"« {text.replace('--', '- -')} » {score:.2f}" for text, score in page.noise)
            blocks.append(f"<!-- bruit probable, hors du texte : {fragments} -->")
    return "\n\n".join(blocks) + "\n"


def _pdf_pages(path: Path, on_page: Callable[[int, int], None]) -> list[Page]:
    pages = []
    pdf = pdfium.PdfDocument(path)
    try:
        on_page(0, len(pdf))
        for number, page in enumerate(pdf, start=1):
            textpage = page.get_textpage()
            text = _normalize(textpage.get_text_bounded())
            if len(text) >= MIN_TEXT_CHARS:
                pieces = _text_layer_pieces(textpage, page.get_height())
                pages.append(Page(number, layout.paragraphs(pieces) or text, "text"))
            else:
                image = page.render(scale=OCR_DPI / 72).to_pil()
                pages.append(_ocr_page(number, image))
            textpage.close()
            page.close()
            on_page(number, len(pdf))
    finally:
        pdf.close()
    return pages


def _text_layer_pieces(textpage, page_height: float) -> list[layout.Piece]:
    pieces = []
    for index in range(textpage.count_rects()):
        left, bottom, right, top = textpage.get_rect(index)
        text = " ".join(textpage.get_text_bounded(left, bottom, right, top).split())
        if text:
            pieces.append(layout.Piece(text, left, page_height - top, right, page_height - bottom))
    return pieces


def ocr_pieces(image: Image.Image, engine=None) -> list[layout.Piece]:
    """What the OCR reads on a page, piece by piece, with boxes and scores."""
    result = (engine or _ocr_engine())(image.convert("RGB"))
    pieces = []
    boxes = result.boxes if result.boxes is not None else ()  # a numpy array: no « or () »
    for text, score, box in zip(result.txts or (), result.scores or (), boxes):
        if text.strip():
            xs, ys = [point[0] for point in box], [point[1] for point in box]
            pieces.append(layout.Piece(" ".join(text.split()), min(xs), min(ys), max(xs), max(ys), float(score)))
    return pieces


def _ocr_page(number: int, image: Image.Image, engine=None) -> Page:
    pieces = ocr_pieces(image, engine)
    # Almost no ink and nothing RapidOCR would call text: specks read as letters.
    if all(piece.score < TEXT_SCORE for piece in pieces) and is_blank(image):
        return Page(number, "", "blank")
    noise = [(piece.text, piece.score) for piece in layout.reading_order(pieces) if is_noise(piece)]
    pieces = [piece for piece in pieces if not is_noise(piece)]
    if not pieces:
        return Page(number, "", "ocr", 0.0, 0, noise)

    def render(piece: layout.Piece, line: int) -> str:
        if piece.score >= LOW_CONFIDENCE:
            return piece.text
        # Highlighted in Obsidian; the comment tells how sure, and where to look in the original.
        return f"=={piece.text}==<!-- illisible ? confiance {piece.score:.2f} · ligne {line}/{len(pieces)} -->"

    text = layout.paragraphs(pieces, render)
    return Page(number, text, "ocr", min(piece.score for piece in pieces), len(pieces), noise)


def is_noise(piece: layout.Piece) -> bool:
    return piece.score < TEXT_SCORE and len(piece.text) <= NOISE_MAX_CHARS


def is_blank(image: Image.Image) -> bool:
    gray = image.convert("L")
    small = gray.reduce(max(1, gray.width // BLANK_WIDTH))
    x, y = int(small.width * BLANK_MARGIN), int(small.height * BLANK_MARGIN)
    small = small.crop((x, y, small.width - x, small.height - y))
    paper = ImageStat.Stat(small).median[0]
    if paper < MIN_PAPER:
        return False
    ink = sum(small.histogram()[:int(paper - INK_CONTRAST)])
    return ink <= BLANK_MAX_INK * small.width * small.height


def _docx_text(path: Path) -> str:
    import docx
    from docx.table import Table

    document = docx.Document(str(path))
    blocks = []
    header = "\n".join(p.text.strip() for p in document.sections[0].header.paragraphs if p.text.strip())
    if header:
        blocks.append(f"<!-- en-tête -->\n{header}")
    for item in document.iter_inner_content():
        if isinstance(item, Table):
            blocks.append(_markdown_table([[cell.text for cell in row.cells] for row in item.rows]))
            continue
        text = item.text.strip()
        if not text:
            continue
        style = (item.style.name if item.style is not None else "") or ""
        if heading := re.match(r"(?:heading|titre)\s*(\d)", style, re.IGNORECASE):
            text = "#" * min(int(heading.group(1)), 6) + " " + text
        elif style.lower() in {"title", "titre"}:
            text = "# " + text
        elif re.match(r"list|liste", style, re.IGNORECASE) or item._p.pPr is not None and item._p.pPr.numPr is not None:
            text = "- " + text
        blocks.append(text)
    # Consecutive list items form one list.
    return re.sub(r"(^- .*)\n\n(?=- )", r"\1\n", "\n\n".join(blocks), flags=re.MULTILINE)


def _markdown_table(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    cells = [[" ".join(c.split()).replace("|", "\\|") for c in row] + [""] * (width - len(row)) for row in rows]
    lines = ["| " + " | ".join(cells[0]) + " |", "|" + " --- |" * width]
    lines += ["| " + " | ".join(row) + " |" for row in cells[1:]]
    return "\n".join(lines)


MAIL_HEADERS = [("From", "De"), ("To", "À"), ("Cc", "Cc"), ("Date", "Date"), ("Subject", "Objet")]


def _read_mail(path: Path) -> EmailMessage:
    with open(path, "rb") as file:
        return BytesParser(policy=policy.default).parse(file)


def _mail_text(message: EmailMessage) -> str:
    lines = [f"**{label} :** {_mail_header(message, name)}" for name, label in MAIL_HEADERS if message[name]]
    blocks = ["  \n".join(lines)]
    body = message.get_body(preferencelist=("plain", "html"))
    if body is not None:
        content = body.get_content()
        text = html_to_text(content) if body.get_content_subtype() == "html" else content
        blocks.append(_normalize(text))
    names = [part.get_filename() or "sans nom" for part in _attachments(message)]
    if names:
        blocks.append("**Pièces jointes :** " + ", ".join(names))
    return "\n\n".join(block for block in blocks if block)


def _mail_header(message: EmailMessage, name: str) -> str:
    header = message[name]
    if name == "Date" and getattr(header, "datetime", None):
        return header.datetime.strftime("%Y-%m-%d %H:%M")  # in the sender's time zone
    return str(header)


def mail_attachments(path: Path) -> list[tuple[str, bytes]]:
    """The attached files of an e-mail (inline images such as logos are left out)."""
    return [(part.get_filename() or "sans nom", part.get_content())
            for part in _attachments(_read_mail(path)) if isinstance(part.get_content(), bytes)]


def _attachments(message: EmailMessage):
    return (part for part in message.iter_attachments() if part.get_content_disposition() == "attachment")


class _HTMLText(HTMLParser):
    BLOCKS = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "ul", "ol", "blockquote"}
    SKIP = {"script", "style", "head", "title"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skipping = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skipping += 1
        elif tag in self.BLOCKS:
            self.parts.append("\n- " if tag == "li" else "\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skipping = max(0, self.skipping - 1)
        elif tag in self.BLOCKS and tag != "li":  # list items stay on consecutive lines
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skipping:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _HTMLText()
    parser.feed(html)
    parser.close()
    lines = (" ".join(line.split()) for line in "".join(parser.parts).split("\n"))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _normalize(text: str) -> str:
    lines = (line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"))
    return "\n".join(lines).strip()


@cache
def _ocr_engine():
    from rapidocr import RapidOCR

    return RapidOCR(params={"Global.log_level": "error", "Global.text_score": 0.0})
