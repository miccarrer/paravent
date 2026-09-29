"""Tools to measure the conversion on real documents, without showing them to anyone.

Everything they write goes to ``_banc/`` at the root of the corpus (outside
``corpus/``, hence never in the mirror); what they print is figures only, so
that it can be pasted to a helper.

    uv run python scripts/banc.py lignes --corpus ~/Paravent/Maman
        Picks long OCR lines flagged « illisible ? », finds each one in its
        original, and writes « _banc/Contrôle des lignes.md »: for each line,
        the piece of the original, the text read, three boxes to tick.
    uv run python scripts/banc.py lignes --compter --corpus ~/Paravent/Maman
        Counts the ticks, per range of confidence.
    uv run python scripts/banc.py preparer --corpus ~/Paravent/Maman
        Picks varied pages and writes, for each, a note with the image of the
        page and its current text, to be corrected by hand: the reference.
    uv run python scripts/banc.py comparer --corpus ~/Paravent/Maman
        Runs every candidate chain on the corrected pages and prints their
        error rates, table scores and time per page.
"""

from __future__ import annotations

import argparse
import difflib
import json
import logging
import os
import random
import re
import statistics
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

import numpy as np
import pypdfium2 as pdfium
from PIL import Image

from paravent import corpus, extract, layout

BENCH = "_banc"
CHECK_NOTE = "Contrôle des lignes.md"
LONG = 15  # characters: shorter lines say little about the reading of real text
RANGES = [(0.50, "< 0,50"), (0.70, "0,50–0,70"), (extract.LOW_CONFIDENCE, "0,70–0,80")]
ANSWERS = ["juste", "un peu fausse", "très fausse"]
PER_DOCUMENT = 2  # at most, so that one bad scan does not make the whole sample

_PAGE = re.compile(r"^<!-- page (\d+) · \w+[^\n]*-->$", re.MULTILINE)
_NEW_FLAG = re.compile(r"==(.*?)==<!-- illisible \? confiance ([\d.]+) · ligne \d+/\d+ -->", re.DOTALL)
_OLD_FLAG = re.compile(r"<!-- illisible \? confiance ([\d.]+) -->")
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


@dataclass
class Line:
    document: corpus.Document
    page: int
    text: str
    confidence: float

    @property
    def range(self) -> str:
        return next(label for bound, label in RANGES if self.confidence < bound)


def flagged_lines(c: corpus.Corpus) -> list[Line]:
    found = []
    for document in c.documents(corpus.DONE, corpus.REVIEW):
        if document.markdown is None:
            continue
        markdown = (c.root / document.markdown).read_text(encoding="utf-8")
        markers = list(_PAGE.finditer(markdown))
        for marker, following in zip(markers, [*markers[1:], None]):
            body = markdown[marker.end():following.start() if following else len(markdown)]
            page = int(marker.group(1))
            pairs = [(text, score) for text, score in _NEW_FLAG.findall(body)]
            if not pairs:  # converted before paragraphs were rebuilt: one line per paragraph
                pairs = [(_COMMENT.sub("", paragraph).replace("==", ""), flag.group(1))
                         for paragraph in re.split(r"\n\s*\n", body) if (flag := _OLD_FLAG.search(paragraph))]
            for text, score in pairs:
                text = " ".join(text.split())
                if len(text) > LONG and float(score) < extract.LOW_CONFIDENCE:
                    found.append(Line(document, page, text, float(score)))
    return found


def sample(lines: list[Line], count: int, seed: int) -> list[Line]:
    """Spread over the ranges of confidence and over documents, in random order."""
    rng = random.Random(seed)
    by_range = defaultdict(list)
    for line in rng.sample(lines, len(lines)):
        by_range[line.range].append(line)
    chosen, per_document = [], Counter()
    while len(chosen) < count and any(by_range.values()):
        for _, label in RANGES:
            while by_range[label]:
                line = by_range[label].pop()
                if per_document[line.document.sha256] < PER_DOCUMENT:
                    chosen.append(line)
                    per_document[line.document.sha256] += 1
                    break
            if len(chosen) == count:
                break
    return chosen


def page_image(root: Path, line: Line) -> Image.Image:
    original = root / line.document.original
    if original.suffix.lower() != ".pdf":
        return Image.open(original).convert("RGB")
    pdf = pdfium.PdfDocument(original)
    try:
        return pdf[line.page - 1].render(scale=extract.OCR_DPI / 72).to_pil().convert("RGB")
    finally:
        pdf.close()


def locate_line(image: Image.Image, text: str) -> tuple[int, int, int, int] | None:
    """The box of ``text`` in the page, read again by the OCR."""
    result = extract._ocr_engine()(image)
    best, box = 0.0, None
    boxes = result.boxes if result.boxes is not None else ()
    for read, points in zip(result.txts or (), boxes):
        ratio = difflib.SequenceMatcher(None, " ".join(read.split()), text).ratio()
        if ratio > best:
            best, box = ratio, points
    if box is None or best < 0.8:
        return None
    xs, ys = [p[0] for p in box], [p[1] for p in box]
    return int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))


def write_check(root: Path, count: int, seed: int, replace: bool) -> int:
    bench = root / BENCH
    note = bench / CHECK_NOTE
    if note.exists() and not replace and "[x]" in note.read_text(encoding="utf-8").lower():
        print(f"« {CHECK_NOTE} » a déjà des cases cochées : « --compter » pour les compter, "
              "« --remplacer » pour repartir de zéro.", file=sys.stderr)
        return 1
    with corpus.Corpus(root) as c:
        lines = flagged_lines(c)
    print(f"Lignes longues « illisible ? » dans le corpus : {len(lines)}")
    images = bench / "lignes"
    images.mkdir(parents=True, exist_ok=True)
    blocks, number = [], 0
    for line in sample(lines, len(lines), seed):
        if number == count:
            break
        image = page_image(root, line)
        box = locate_line(image, line.text)
        if box is None:
            continue  # the Markdown was edited, or the OCR reads it otherwise now
        number += 1
        x0, y0, x1, y1 = box
        image.crop((max(x0 - 40, 0), max(y0 - 25, 0), x1 + 40, y1 + 25)).save(images / f"ligne-{number:02d}.png")
        blocks.append(f"## Ligne {number} · confiance {line.confidence:.2f}\n\n"
                      f"![[{BENCH}/lignes/ligne-{number:02d}.png]]\n\n"
                      f"Texte lu par l'OCR :\n\n> {line.text}\n\n"
                      + "".join(f"- [ ] {answer}\n" for answer in ANSWERS)
                      + f"\n<!-- banc · confiance:{line.confidence:.2f} -->\n")
        print(f"  {number}/{count}", flush=True)
    intro = ("# Contrôle des lignes « illisible ? »\n\n"
             "Pour chaque ligne, comparez l'image (l'original) au texte lu, et cochez **une** case :\n\n"
             "- **juste** : identique à l'image (un espace en plus ou en moins ne compte pas) ;\n"
             "- **un peu fausse** : 1 ou 2 caractères faux (accent, lettre, chiffre, ponctuation) ;\n"
             "- **très fausse** : davantage, ou un mot manquant, inventé ou méconnaissable.\n\n"
             "Puis lancez « uv run python scripts/banc.py lignes --compter » : seuls des chiffres sortent.\n\n")
    note.write_text(intro + "\n".join(blocks), encoding="utf-8")
    print(f"Note écrite : {note.relative_to(root).as_posix()} ({number} lignes)")
    return 0


def count_check(root: Path) -> int:
    note = root / BENCH / CHECK_NOTE
    if not note.exists():
        print(f"Pas de « {CHECK_NOTE} » : lancez d'abord « banc.py lignes ».", file=sys.stderr)
        return 1
    table = defaultdict(Counter)
    for section in note.read_text(encoding="utf-8").split("\n## ")[1:]:
        confidence = re.search(r"<!-- banc · confiance:([\d.]+) -->", section)
        if not confidence:
            continue
        label = next(label for bound, label in RANGES if float(confidence.group(1)) < bound)
        ticked = [answer for answer in ANSWERS if re.search(rf"^- \[[xX]\] {answer}$", section, re.MULTILINE)]
        table[label][ticked[0] if len(ticked) == 1 else "sans réponse" if not ticked else "plusieurs cases"] += 1
    columns = [*ANSWERS, "sans réponse", "plusieurs cases"]
    columns = [col for col in columns if col in ANSWERS or any(table[label][col] for label in table)]
    print("confiance".ljust(12) + "".join(col.rjust(16) for col in columns))
    total = Counter()
    for _, label in RANGES:
        total.update(table[label])
        print(label.ljust(12) + "".join(str(table[label][col]).rjust(16) for col in columns))
    print("total".ljust(12) + "".join(str(total[col]).rjust(16) for col in columns))
    return 0


# --- reference pages ---------------------------------------------------------------

REFERENCES = "references"
INDEX_NOTE = "Pages de référence.md"
DONE_BOX = "- [ ] J'ai fini de corriger cette page"
CUT = "<!-- ↓ Corrigez le texte sous cette ligne. Ne modifiez pas ce qui est au-dessus. -->"
# Pages wanted per kind, for ten pages: text layers read almost perfectly, the OCR is where it matters.
QUOTAS = {"texte": 2, "OCR net": 2, "OCR moyen": 3, "OCR difficile": 3}
DIFFICULT = 0.10  # share of flagged lines from which an OCR page is « difficile »
DISPLAY_DPI = 150

_MARKER = re.compile(r"^<!-- page (\d+) · (\w+)(?: · (\d+) lignes)?(?: · \d+ fragments)?"
                     r"(?: · confiance min [\d.]+)? -->$", re.MULTILINE)


@dataclass
class PageRef:
    document: corpus.Document
    page: int
    kind: str
    text: str  # the current conversion, cleaned: the starting point of the reference


def clean(body: str) -> str:
    """A page's Markdown without Paravent's comments and highlights."""
    text = _COMMENT.sub("", body).replace("==", "")
    return re.sub(r"\n{3,}", "\n\n", "\n".join(line.rstrip() for line in text.splitlines())).strip()


def candidate_pages(c: corpus.Corpus) -> list[PageRef]:
    found = []
    for document in c.documents(corpus.DONE, corpus.REVIEW):
        if document.markdown is None:
            continue
        markdown = (c.root / document.markdown).read_text(encoding="utf-8")
        markers = list(_MARKER.finditer(markdown))
        for marker, following in zip(markers, [*markers[1:], None]):
            body = markdown[marker.end():following.start() if following else len(markdown)]
            text = clean(body)
            if not text or marker.group(2) not in ("text", "ocr"):
                continue
            if marker.group(2) == "text":
                kind = "texte"
            else:
                lines = int(marker.group(3)) if marker.group(3) else len(re.split(r"\n\s*\n", text))
                flagged = len(re.findall(r"<!-- illisible \?", body))
                share = flagged / max(lines, 1)
                kind = "OCR net" if not flagged else "OCR moyen" if share < DIFFICULT else "OCR difficile"
            found.append(PageRef(document, int(marker.group(1)), kind, text))
    return found


def pick_pages(pages: list[PageRef], count: int, seed: int, taken: set[tuple[str, int]]) -> list[PageRef]:
    rng = random.Random(seed)
    pages = [p for p in rng.sample(pages, len(pages)) if (p.document.sha256, p.page) not in taken]
    total = sum(QUOTAS.values())
    wanted = {kind: round(quota * count / total) for kind, quota in QUOTAS.items()}
    chosen, per_document = [], Counter()
    for strict in (True, False):  # quotas first, then whatever is left
        for page in pages:
            if len(chosen) == count:
                break
            if page in chosen or per_document[page.document.sha256] >= PER_DOCUMENT:
                continue
            if strict and sum(p.kind == page.kind for p in chosen) >= wanted[page.kind]:
                continue
            chosen.append(page)
            per_document[page.document.sha256] += 1
    return chosen


def render_page(root: Path, original: str, page: int, dpi: int) -> Image.Image:
    path = root / original
    if path.suffix.lower() != ".pdf":
        return Image.open(path).convert("RGB")
    pdf = pdfium.PdfDocument(path)
    try:
        return pdf[page - 1].render(scale=dpi / 72).to_pil().convert("RGB")
    finally:
        pdf.close()


def write_references(root: Path, count: int, seed: int, add: bool) -> int:
    folder = root / BENCH / REFERENCES
    catalogue = folder / "pages.json"
    known = json.loads(catalogue.read_text(encoding="utf-8")) if catalogue.exists() else []
    if known and not add:
        print(f"Des pages de référence existent déjà ({len(known)}) : « --ajouter » pour en ajouter d'autres.",
              file=sys.stderr)
        return 1
    with corpus.Corpus(root) as c:
        pages = candidate_pages(c)
    taken = {(entry["sha256"], entry["page"]) for entry in known}
    chosen = pick_pages(pages, count, seed, taken)
    print(f"Pages candidates : {len(pages)} · " + " · ".join(
        f"{kind} {sum(p.kind == kind for p in pages)}" for kind in QUOTAS))
    folder.mkdir(parents=True, exist_ok=True)
    for number, page in enumerate(chosen, start=len(known) + 1):
        name = f"page-{number:02d}"
        render_page(root, page.document.original, page.page, DISPLAY_DPI).save(folder / f"{name}.jpg", quality=85)
        (folder / f"{name}.md").write_text(
            f"# {name} · {page.kind}\n\n{DONE_BOX}\n\n![[{BENCH}/{REFERENCES}/{name}.jpg]]\n\n{CUT}\n\n{page.text}\n",
            encoding="utf-8")
        known.append({"id": name, "sha256": page.document.sha256, "original": page.document.original,
                      "page": page.page, "kind": page.kind})
    catalogue.write_text(json.dumps(known, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (root / BENCH / INDEX_NOTE).write_text(REFERENCE_GUIDE + "\n".join(
        f"- [[{BENCH}/{REFERENCES}/{entry['id']}|{entry['id']}]] · {entry['kind']}" for entry in known) + "\n",
        encoding="utf-8")
    print(f"Pages préparées : {len(chosen)} · " + " · ".join(
        f"{kind} {sum(p.kind == kind for p in chosen)}" for kind in QUOTAS))
    print(f"À corriger dans Obsidian : {BENCH}/{INDEX_NOTE}")
    return 0


REFERENCE_GUIDE = f"""# Pages de référence

Chaque page ci-dessous contient l'image de l'original et, sous la ligne de commentaire, le texte
lu aujourd'hui par Paravent. **Corrigez ce texte pour qu'il soit exactement celui de l'image**,
puis cochez « J'ai fini de corriger cette page ». Seules les pages cochées sont comparées.

- Tout le texte **imprimé** visible, dans l'ordre où on le lit : en-têtes, adresses, tampons et
  pieds de page compris. Le manuscrit : recopiez-le s'il est lisible pour vous, sinon supprimez-le.
- Un paragraphe par bloc, séparé du suivant par une ligne vide ; les retours à la ligne à
  l'intérieur d'un paragraphe ne comptent pas.
- **Tableaux** : une ligne par rangée, les cellules entre des barres, sans ligne de séparation :
  `| 05/01/2026 | Pension de base | 812,40 € |`. Une cellule vide : `|  |`.
- La mise en forme (titres, gras) ne compte pas : seul le texte compte.
- Lisez vraiment chaque ligne : une erreur laissée dans la référence avantage la chaîne actuelle.
- Astuce : ouvrez l'image dans un panneau à côté (clic droit sur l'image, « Ouvrir à droite »).

"""


# --- candidate chains ----------------------------------------------------------------

@dataclass
class Chain:
    name: str
    about: str
    dpi: int = extract.OCR_DPI
    params: dict = field(default_factory=dict)
    tables: bool = False
    layout: bool = False
    pdf_order: bool = False  # text layers read in the file's order, not rebuilt from positions
    vision: str | None = None  # a vision model served by the local Ollama: it reads the whole page
    vision_side: int | None = None  # longest side of the image sent, in pixels (None: as rendered)


def chains() -> dict[str, Chain]:
    from rapidocr import LangRec, ModelType, OCRVersion

    medium = {"Det.model_type": ModelType.MEDIUM, "Rec.model_type": ModelType.MEDIUM}
    latin = {"Rec.ocr_version": OCRVersion.PPOCRV5, "Rec.lang_type": LangRec.LATIN, "Rec.model_type": ModelType.MOBILE}
    full = {"Global.max_side_len": 5000}  # RapidOCR shrinks any image beyond 2000 px: an A4 page at 200 ppp too
    return {chain.name: chain for chain in [
        Chain("actuel", "Paravent aujourd'hui (PP-OCRv6 small, 200 ppp réduits à ~170 par RapidOCR)"),
        Chain("200-plein", "la même, sans la réduction à 2000 pixels", params=full),
        Chain("300-plein", "lue à 300 ppp, sans réduction", dpi=300, params=full),
        Chain("ordre-pdf", "actuel, mais couche texte dans l'ordre du fichier (pages texte seulement)",
              pdf_order=True),
        Chain("v6-medium", "PP-OCRv6 medium (détection et lecture)", params=medium),
        Chain("latin", "lecture PP-OCRv5 latin", params=latin),
        Chain("tableaux", "actuel + tableaux (PP layout_table + SLANet+)", tables=True),
        Chain("mise-en-page", "actuel + PP-DocLayout v2 + tableaux", tables=True, layout=True),
    ]}


# OCR models are trained on their own short instruction; general models get a strict one.
VISION_PROMPTS = {"glm-ocr": "Text Recognition:"}
TRANSCRIBE = ("Transcris exactement tout le texte imprimé de cette page, dans l'ordre de lecture. Ne corrige "
              "rien, n'ajoute rien, ne résume pas. Les tableaux en Markdown (| a | b |). Réponds uniquement "
              "par la transcription.")
VISION_TIMEOUT = 900  # seconds for one page: a model spilling out of the GPU is slow


def vision_chain(name: str) -> Chain:
    """« vision:<model> », or « vision:<model>@<pixels> » to send a smaller image (fewer image tokens)."""
    model, _, side = name.split(":", 1)[1].partition("@")
    about = f"modèle de vision {model}, via l'Ollama local" + (f", image réduite à {side} px" if side else "")
    return Chain(name, about, vision=model, vision_side=int(side) if side else None)


class Runner:
    """Runs a chain on a page; engines and models are loaded once."""

    def __init__(self):
        self.engines, self.models, self.thinking = {}, {}, {}

    def ocr(self, chain: Chain):
        key = repr(sorted((k, str(v)) for k, v in chain.params.items()))
        if key not in self.engines:
            from rapidocr import RapidOCR

            self.engines[key] = RapidOCR(params={"Global.log_level": "error", "Global.text_score": 0.0,
                                                 **chain.params})
        return self.engines[key]

    def model(self, name: str):
        if name not in self.models:
            if name == "slanet":
                from rapid_table import ModelType, RapidTable, RapidTableInput

                self.models[name] = RapidTable(RapidTableInput(model_type=ModelType.SLANETPLUS))
            else:
                from rapid_layout import RapidLayout

                self.models[name] = RapidLayout(model_type=name)
        return self.models[name]

    def run(self, chain: Chain, root: Path, original: str, page: int) -> str:
        image = render_page(root, original, page, chain.dpi)
        if chain.vision:
            if chain.vision_side:
                image.thumbnail((chain.vision_side, chain.vision_side), Image.Resampling.LANCZOS)
            return self.read_with_vision(chain.vision, image)
        if chain.pdf_order and (text := self.text_in_file_order(root, original, page)):
            return text
        pieces = self.text_layer(root, original, page, chain.dpi)
        if pieces is None:
            pieces = [p for p in extract.ocr_pieces(image, self.ocr(chain)) if not extract.is_noise(p)]
        if not chain.tables:
            return layout.paragraphs(pieces)
        array = np.array(image)
        if chain.layout:
            out = self.model("pp_doc_layoutv2")(array)
            blocks = [] if out.boxes is None else list(zip(out.boxes, out.class_names))
        else:
            out = self.model("pp_layout_table")(array)
            blocks = [] if out.boxes is None else [(box, "table") for box in out.boxes]
        return self.assemble(array, pieces, blocks)

    def read_with_vision(self, model: str, image: Image.Image) -> str:
        import base64
        import io

        from paravent import ia

        config = ia.load_config() or ia.Config("http://localhost:11434", model)
        config = ia.Config(config.url, model, VISION_TIMEOUT)  # checked local before every request
        if model not in self.thinking:
            capabilities = ia._request(config, "/api/show", {"model": model}).get("capabilities", [])
            self.thinking[model] = "thinking" in capabilities
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        prompt = VISION_PROMPTS.get(model.split(":")[0].split("/")[-1], TRANSCRIBE)
        payload = {"model": model, "stream": False, "options": {"temperature": 0, "num_ctx": 16384},
                   "messages": [{"role": "user", "content": prompt,
                                 "images": [base64.b64encode(buffer.getvalue()).decode("ascii")]}]}
        if self.thinking[model]:
            payload["think"] = False
        content = ia._request(config, "/api/chat", payload).get("message", {}).get("content", "")
        return re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()

    @staticmethod
    def text_in_file_order(root: Path, original: str, page: int) -> str | None:
        path = root / original
        if path.suffix.lower() != ".pdf":
            return None
        pdf = pdfium.PdfDocument(path)
        try:
            text = extract._normalize(pdf[page - 1].get_textpage().get_text_bounded())
            return text if len(text) >= extract.MIN_TEXT_CHARS else None
        finally:
            pdf.close()

    @staticmethod
    def text_layer(root: Path, original: str, page: int, dpi: int) -> list[layout.Piece] | None:
        path = root / original
        if path.suffix.lower() != ".pdf":
            return None
        pdf = pdfium.PdfDocument(path)
        try:
            pdf_page = pdf[page - 1]
            textpage = pdf_page.get_textpage()
            if len(textpage.get_text_bounded().strip()) < extract.MIN_TEXT_CHARS:
                return None
            scale = dpi / 72
            return [layout.Piece(p.text, p.left * scale, p.top * scale, p.right * scale, p.bottom * scale, 1.0)
                    for p in extract._text_layer_pieces(textpage, pdf_page.get_height())]
        finally:
            pdf.close()

    def assemble(self, array, pieces: list[layout.Piece], blocks: list) -> str:
        """Blocks in reading order: tables read by SLANet+, titles marked, the rest in paragraphs."""
        placed, loose = defaultdict(list), []
        for piece in pieces:
            cx, cy = (piece.left + piece.right) / 2, (piece.top + piece.bottom) / 2
            inside = [i for i, (box, _) in enumerate(blocks) if box[0] <= cx <= box[2] and box[1] <= cy <= box[3]]
            if inside:
                placed[min(inside, key=lambda i: _area(blocks[i][0]))].append(piece)  # the innermost block
            else:
                loose.append(piece)
        items = [(blocks[i][0], blocks[i][1], group) for i, group in placed.items()]
        items += [((p.left, p.top, p.right, p.bottom), None, [p]) for p in loose]
        out, run = [], []
        for _, kind, group in _reading_order(items):
            if kind is None:
                run += group
                continue
            if run:
                out.append(layout.paragraphs(run))
                run = []
            if kind == "table":
                out.append(self.table(array, blocks, group) or layout.paragraphs(group))
            elif kind in ("paragraph_title", "doc_title", "title"):
                out.append("## " + " ".join(p.text for p in layout.reading_order(group)))
            else:
                out.append(layout.paragraphs(group))
        if run:
            out.append(layout.paragraphs(run))
        return "\n\n".join(block for block in out if block)

    def table(self, array, blocks, group: list[layout.Piece]) -> str | None:
        x0 = int(max(min(p.left for p in group) - 10, 0))
        y0 = int(max(min(p.top for p in group) - 10, 0))
        x1, y1 = int(max(p.right for p in group) + 10), int(max(p.bottom for p in group) + 10)
        crop = array[y0:y1, x0:x1]
        corners = [[[p.left - x0, p.top - y0], [p.right - x0, p.top - y0],
                    [p.right - x0, p.bottom - y0], [p.left - x0, p.bottom - y0]] for p in group]
        boxes = np.array(corners, dtype=np.float32)
        result = self.model("slanet")([crop], ocr_results=[(boxes, tuple(p.text for p in group),
                                                            tuple(p.score or 1.0 for p in group))])
        rows = table_rows(result.pred_htmls[0]) if result.pred_htmls else []
        return "\n".join("| " + " | ".join(row) + " |" for row in rows) if rows else None


def _reading_order(items: list) -> list:
    """Items (box, kind, pieces) row by row: those overlapping vertically form a row, read left to right."""
    rows: list[list] = []
    for item in sorted(items, key=lambda item: (item[0][1] + item[0][3]) / 2):
        box = item[0]
        if rows:
            last = rows[-1][-1][0]
            overlap = min(last[3], box[3]) - max(last[1], box[1])
            if overlap >= 0.5 * min(last[3] - last[1], box[3] - box[1]):
                rows[-1].append(item)
                continue
        rows.append([item])
    return [item for row in rows for item in sorted(row, key=lambda item: item[0][0])]


def _area(box) -> float:
    return (box[2] - box[0]) * (box[3] - box[1])


class _Cells(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows, self.cell = [], None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.rows.append([])
        elif tag in ("td", "th"):
            self.cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.cell is not None and self.rows:
            self.rows[-1].append(" ".join("".join(self.cell).split()))
            self.cell = None

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)


def table_rows(html: str) -> list[list[str]]:
    parser = _Cells()
    parser.feed(html)
    return [row for row in parser.rows if any(row)]


# --- metrics -----------------------------------------------------------------------------

_SEPARATOR_ROW = re.compile(r"^\s*\|?(\s*:?-{3,}:?\s*\|)+\s*:?-*:?\s*$")


def plain(text: str) -> str:
    """Only the words: comments, highlights, titles, table bars and line breaks set aside."""
    text = _COMMENT.sub(" ", text).replace("==", "").replace("**", "")
    lines = [re.sub(r"^#+\s+", "", line).replace("|", " ") for line in text.splitlines()
             if not _SEPARATOR_ROW.match(line)]
    return " ".join(" ".join(lines).split())


def markdown_tables(text: str) -> list[list[list[str]]]:
    tables, current = [], []
    for line in text.splitlines():
        if line.strip().startswith("|") and not _SEPARATOR_ROW.match(line):
            current.append([" ".join(cell.split()) for cell in line.strip().strip("|").split("|")])
        elif current and not _SEPARATOR_ROW.match(line):
            tables.append(current)
            current = []
    return tables + [current] if current else tables


def table_score(reference: list, candidate: list) -> tuple[int, int, int]:
    """Tables found, cells right (same row and column), cells in the reference."""
    found = right = total = 0
    for ref in reference:
        cells = sum(len(row) for row in ref)
        total += cells
        best = max((sum(1 for r, row in enumerate(ref) for k, cell in enumerate(row)
                        if r < len(hyp) and k < len(hyp[r]) and hyp[r][k] == cell) for hyp in candidate), default=0)
        found += best > 0
        right += best
    return found, right, total


def errors(reference: str, candidate: str) -> tuple[float, float, float]:
    """Character, character without spaces, and word error rates."""
    from rapidfuzz.distance import Levenshtein

    ref, hyp = plain(reference), plain(candidate)
    squeeze = lambda s: "".join(s.split())
    return (Levenshtein.distance(ref, hyp) / max(len(ref), 1),
            Levenshtein.distance(squeeze(ref), squeeze(hyp)) / max(len(squeeze(ref)), 1),
            Levenshtein.distance(ref.split(), hyp.split()) / max(len(ref.split()), 1))


def words_found(reference: str, candidate: str) -> tuple[float, float]:
    """Order set aside: share of the reference's words found, share of the candidate's words in excess."""
    ref, hyp = Counter(plain(reference).split()), Counter(plain(candidate).split())
    common = sum((ref & hyp).values())
    return common / max(sum(ref.values()), 1), 1 - common / max(sum(hyp.values()), 1)


def compare(root: Path, names: list[str] | None, only: list[str] | None = None) -> int:
    folder = root / BENCH / REFERENCES
    catalogue = folder / "pages.json"
    if not catalogue.exists():
        print("Pas de pages de référence : lancez d'abord « banc.py preparer ».", file=sys.stderr)
        return 1
    pages = []
    for entry in json.loads(catalogue.read_text(encoding="utf-8")):
        note = (folder / f"{entry['id']}.md").read_text(encoding="utf-8")
        if DONE_BOX.replace("[ ]", "[x]") not in note.replace("[X]", "[x]") or CUT not in note:
            continue
        if only and entry["id"] not in only:
            continue
        pages.append((entry, note.split(CUT, 1)[1].strip()))
    available = chains()
    selected = [vision_chain(name) if name.startswith("vision:") else available[name]
                for name in (names or available)]
    print(f"Pages corrigées : {len(pages)} · " + " · ".join(
        f"{kind} {sum(e['kind'] == kind for e, _ in pages)}" for kind in QUOTAS)
        + f" · caractères de référence : {sum(len(plain(ref)) for _, ref in pages)}")
    if not pages:
        return 1
    runner = Runner()
    results = {}
    for chain in selected:
        runner.run(chain, root, pages[0][0]["original"], pages[0][0]["page"])  # models loaded, not timed
        rows = []
        for entry, reference in pages:
            start = time.perf_counter()
            text = runner.run(chain, root, entry["original"], entry["page"])
            reference_tables = markdown_tables(reference)
            rows.append((entry, errors(reference, text), table_score(reference_tables, markdown_tables(text)),
                         time.perf_counter() - start, len(reference_tables), words_found(reference, text)))
        results[chain.name] = rows
        print(f"  {chain.name} : fait", flush=True)
    report_comparison(selected, results)
    return 0


def report_comparison(selected: list[Chain], results: dict) -> None:
    pct = lambda value: f"{value * 100:.1f} %".replace(".", ",")
    print("\nchaîne                 err. car.  sans espaces  err. mots   mots retrouvés  mots en trop       tableaux   s/page")
    print("                                                            (ordre ignoré)")
    for chain in selected:
        rows = results[chain.name]
        means = [statistics.mean(row[1][i] for row in rows) for i in range(3)]
        found = sum(row[2][0] for row in rows)
        right, total = sum(row[2][1] for row in rows), sum(row[2][2] for row in rows)
        tables = sum(row[4] for row in rows)
        cells = f"{found}/{tables} · {pct(right / total)}" if total else "—"
        found_words = statistics.mean(row[5][0] for row in rows)
        extra_words = statistics.mean(row[5][1] for row in rows)
        print(f"{chain.name:21s} {pct(means[0]):>10s} {pct(means[1]):>13s} {pct(means[2]):>10s}"
              f" {pct(found_words):>15s} {pct(extra_words):>13s} {cells:>14s} {statistics.mean(row[3] for row in rows):8.1f}")
    print("  tableaux : trouvés / dans la référence · cellules justes (même rangée, même colonne)")
    print("\nErreurs par caractère, page par page :")
    print("page      nature         " + "".join(chain.name[-13:].rjust(14) for chain in selected))
    for index, (entry, *_rest) in enumerate(results[selected[0].name]):
        print(f"{entry['id']:9s} {entry['kind']:14s} "
              + "".join(pct(results[chain.name][index][1][0]).rjust(14) for chain in selected))
    print("\nMots de la référence retrouvés (ordre ignoré), page par page :")
    print("page      nature         " + "".join(chain.name[-13:].rjust(14) for chain in selected))
    for index, (entry, *_rest) in enumerate(results[selected[0].name]):
        print(f"{entry['id']:9s} {entry['kind']:14s} "
              + "".join(pct(results[chain.name][index][5][0]).rjust(14) for chain in selected))
    for chain in selected:
        print(f"  {chain.name} : {chain.about}")


def main(argv: list[str] | None = None) -> int:
    logging.disable(logging.INFO)  # the models' loading messages
    os.environ.setdefault("TQDM_DISABLE", "1")  # RapidTable's progress bar
    parser = argparse.ArgumentParser(prog="banc.py", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    lines = commands.add_parser("lignes", help="contrôler des lignes « illisible ? » à l'œil")
    lines.add_argument("--corpus", type=Path, default=Path.cwd())
    lines.add_argument("--nombre", type=int, default=10)
    lines.add_argument("--graine", type=int, default=1, help="autre valeur : un autre tirage")
    lines.add_argument("--compter", action="store_true", help="compter les cases cochées")
    lines.add_argument("--remplacer", action="store_true", help="refaire la note même si des cases sont cochées")
    prepare = commands.add_parser("preparer", help="choisir des pages à corriger à la main (la référence)")
    prepare.add_argument("--corpus", type=Path, default=Path.cwd())
    prepare.add_argument("--nombre", type=int, default=10)
    prepare.add_argument("--graine", type=int, default=1, help="autre valeur : un autre tirage")
    prepare.add_argument("--ajouter", action="store_true", help="ajouter des pages à celles qui existent")
    comp = commands.add_parser("comparer", help="passer les pages corrigées dans chaque chaîne candidate")
    comp.add_argument("--corpus", type=Path, default=Path.cwd())
    comp.add_argument("--pages", help="seulement ces pages, séparées par des virgules (ex. page-10,page-08)")
    comp.add_argument("--chaines", help="noms séparés par des virgules (défaut : toutes) : " + ", ".join(
        ["actuel", "200-plein", "300-plein", "ordre-pdf", "v6-medium", "latin", "tableaux", "mise-en-page"])
        + " ; et vision:<modèle> pour un modèle de vision de l'Ollama local (ex. vision:glm-ocr:q8_0)")
    args = parser.parse_args(argv)
    root = corpus.find_root(args.corpus)
    if args.command == "preparer":
        return write_references(root, args.nombre, args.graine, args.ajouter)
    if args.command == "comparer":
        return compare(root, args.chaines.split(",") if args.chaines else None,
                       args.pages.split(",") if args.pages else None)
    if args.compter:
        return count_check(root)
    return write_check(root, args.nombre, args.graine, args.remplacer)


if __name__ == "__main__":
    sys.exit(main())
