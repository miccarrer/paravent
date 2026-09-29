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
from dataclasses import dataclass
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from functools import cache
from html.parser import HTMLParser
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image

OCR_DPI = 200
# A PDF page with less extractable text than this is treated as a scan.
MIN_TEXT_CHARS = 25
# OCR lines scored below this are flagged in the Markdown, never silently kept.
LOW_CONFIDENCE = 0.80

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}
SUPPORTED_SUFFIXES = {".pdf", ".docx", ".eml"} | IMAGE_SUFFIXES
UNPAGED = {"docx", "eml"}


class UnsupportedFormat(ValueError):
    pass


@dataclass
class Page:
    number: int
    text: str
    method: str  # "text" or "ocr" for PDF pages and images, "docx" or "eml" for unpaged sources
    min_confidence: float | None = None


def convert(path: Path) -> str:
    return to_markdown(extract_pages(path))


def extract_pages(path: Path) -> list[Page]:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _pdf_pages(path)
    if suffix in IMAGE_SUFFIXES:
        with Image.open(path) as image:
            return [_ocr_page(1, image)]
    if suffix == ".docx":
        return [Page(1, _docx_text(path), "docx")]
    if suffix == ".eml":
        return [Page(1, _mail_text(_read_mail(path)), "eml")]
    raise UnsupportedFormat(f"Format non pris en charge : {path.suffix or path.name}")


def to_markdown(pages: list[Page]) -> str:
    blocks = []
    for page in pages:
        if page.method in UNPAGED:
            blocks += [f"<!-- {page.method} -->", page.text or "<!-- document vide -->"]
            continue
        header = f"<!-- page {page.number} · {page.method}"
        if page.min_confidence is not None:
            header += f" · confiance min {page.min_confidence:.2f}"
        blocks.append(header + " -->")
        blocks.append(page.text if page.text else "<!-- page vide ou illisible -->")
    return "\n\n".join(blocks) + "\n"


def _pdf_pages(path: Path) -> list[Page]:
    pages = []
    pdf = pdfium.PdfDocument(path)
    try:
        for number, page in enumerate(pdf, start=1):
            textpage = page.get_textpage()
            text = _normalize(textpage.get_text_bounded())
            textpage.close()
            if len(text) >= MIN_TEXT_CHARS:
                pages.append(Page(number, text, "text"))
            else:
                image = page.render(scale=OCR_DPI / 72).to_pil()
                pages.append(_ocr_page(number, image))
            page.close()
    finally:
        pdf.close()
    return pages


def _ocr_page(number: int, image: Image.Image) -> Page:
    result = _ocr_engine()(image.convert("RGB"))
    if not result.txts:
        return Page(number, "", "ocr", 0.0)
    lines = []
    for text, score in zip(result.txts, result.scores):
        if score < LOW_CONFIDENCE:
            text += f" <!-- illisible ? confiance {score:.2f} -->"
        lines.append(text)
    # One OCR line per paragraph: paragraph rebuilding comes later.
    return Page(number, "\n\n".join(lines), "ocr", min(result.scores))


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

    return RapidOCR(params={"Global.log_level": "error"})
