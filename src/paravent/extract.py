"""Convert a source document (PDF or image) to Markdown.

PDF pages that carry a text layer are read directly; pages without one
(scans) are rendered and go through OCR. The OCR models ship inside the
rapidocr wheel, so nothing is downloaded at runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image

OCR_DPI = 200
# A PDF page with less extractable text than this is treated as a scan.
MIN_TEXT_CHARS = 25
# OCR lines scored below this are flagged in the Markdown, never silently kept.
LOW_CONFIDENCE = 0.80

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}


class UnsupportedFormat(ValueError):
    pass


@dataclass
class Page:
    number: int
    text: str
    method: str  # "text" or "ocr"
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
    raise UnsupportedFormat(f"Format non pris en charge : {path.suffix or path.name}")


def to_markdown(pages: list[Page]) -> str:
    blocks = []
    for page in pages:
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


def _normalize(text: str) -> str:
    lines = (line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"))
    return "\n".join(lines).strip()


@cache
def _ocr_engine():
    from rapidocr import RapidOCR

    return RapidOCR(params={"Global.log_level": "error"})
