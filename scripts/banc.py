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
"""

from __future__ import annotations

import argparse
import difflib
import random
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image

from paravent import corpus, extract

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="banc.py", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    lines = commands.add_parser("lignes", help="contrôler des lignes « illisible ? » à l'œil")
    lines.add_argument("--corpus", type=Path, default=Path.cwd())
    lines.add_argument("--nombre", type=int, default=10)
    lines.add_argument("--graine", type=int, default=1, help="autre valeur : un autre tirage")
    lines.add_argument("--compter", action="store_true", help="compter les cases cochées")
    lines.add_argument("--remplacer", action="store_true", help="refaire la note même si des cases sont cochées")
    args = parser.parse_args(argv)
    root = corpus.find_root(args.corpus)
    if args.compter:
        return count_check(root)
    return write_check(root, args.nombre, args.graine, args.remplacer)


if __name__ == "__main__":
    sys.exit(main())
