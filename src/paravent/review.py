"""Checking the conversion: figures to share, and a checklist to tick in Obsidian.

``paravent mesures`` prints figures only (counts, weights, ranges), never a
name nor a word of a document, so that its output can be pasted to a cloud
AI or to a helper without disclosing anything. They are computed from the
page markers the conversion writes (``<!-- page N · ocr · confiance min
0.93 -->``), so the database schema is left as it is.

``À vérifier.md``, at the root of the vault (outside ``corpus/``, hence never
in the mirror), lists the doubtful documents with a box to tick once the
Markdown has been compared with its original, then the failures. ``import``
and ``etat`` record the ticked boxes, then rewrite the list.
"""

from __future__ import annotations

import re
import statistics
from collections import Counter
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from urllib.parse import quote

from . import corpus, extract
from .corpus import DONE, FAILED, REVIEW, TODO, Corpus, Document

CHECKLIST = "À vérifier.md"

# A page whose text layer is this short may be a scan carrying only a stamp or
# a footer as text: it went past MIN_TEXT_CHARS, so its content never saw the OCR.
SHORT_TEXT = 200
TEXT_RANGES = [(SHORT_TEXT, f"< {SHORT_TEXT}"), (1000, f"{SHORT_TEXT}–999"), (float("inf"), "≥ 1000")]
CONFIDENCE_RANGES = [(extract.LOW_CONFIDENCE, "< 0,80"), (0.90, "0,80–0,90"), (0.95, "0,90–0,95"),
                     (float("inf"), "≥ 0,95")]

# The flagged OCR lines, described without their text: how long, how sure, where
# in the page, letters or not. Logos, signatures and stamps tend to give short
# fragments without letters at the top or bottom; a pale or small-print scan,
# long lines anywhere.
LENGTH_RANGES = [(4, "1–3"), (16, "4–15"), (float("inf"), "> 15")]
FLAGGED_CONFIDENCE_RANGES = [(0.50, "< 0,50"), (0.70, "0,50–0,70"), (float("inf"), "0,70–0,80")]
EDGE = 0.15  # first and last 15 % of a page's lines: its top and bottom
SHARE_RANGES = [(0.05, "< 5 %"), (0.20, "5–20 %"), (float("inf"), "> 20 %")]

_PAGE = re.compile(r"^<!-- page \d+ · (\w+)(?: · confiance min ([\d.]+))? -->$", re.MULTILINE)
_UNPAGED = re.compile(rf"^<!-- ({'|'.join(sorted(extract.UNPAGED))}) -->$", re.MULTILINE)
_ILLEGIBLE = re.compile(r"<!-- illisible \? confiance ([\d.]+) -->")
_PARAGRAPHS = re.compile(r"\n\s*\n")
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


# --- figures ------------------------------------------------------------------

@dataclass
class Flagged:
    """An OCR line flagged « illisible ? », without its text."""

    chars: int
    confidence: float
    position: float  # 0 = first line of the page, 1 = last
    letters: bool


@dataclass
class Page:
    method: str  # "text", "ocr" or "blank"
    chars: int  # without the comments Paravent adds
    confidence: float | None
    lines: int = 0  # OCR lines (one paragraph each)
    flagged: list[Flagged] = field(default_factory=list)

    @property
    def illegible(self) -> int:
        return len(self.flagged)


def pages_of(markdown: str) -> list[Page]:
    """The pages of a converted PDF or image, read back from their markers."""
    markers = list(_PAGE.finditer(markdown))
    pages = []
    for marker, following in zip(markers, [*markers[1:], None]):
        body = markdown[marker.end():following.start() if following else len(markdown)]
        confidence = marker.group(2)
        page = Page(marker.group(1), len(_COMMENT.sub("", body).strip()), float(confidence) if confidence else None)
        if page.method == "ocr":
            lines = [line for line in _PARAGRAPHS.split(body) if _COMMENT.sub("", line).strip()]
            page.lines = len(lines)
            for index, line in enumerate(lines):
                if flag := _ILLEGIBLE.search(line):
                    text = _COMMENT.sub("", line).strip()
                    page.flagged.append(Flagged(len(text), float(flag.group(1)), index / max(len(lines) - 1, 1),
                                                any(char.isalpha() for char in text)))
        pages.append(page)
    return pages


@dataclass
class Measures:
    formats: dict[str, Counter] = field(default_factory=dict)  # format → status → documents
    weights: Counter = field(default_factory=Counter)  # format → bytes of originals
    missing_originals: int = 0
    attachments: int = 0
    placement: Counter = field(default_factory=Counter)  # à ranger / rangés / introuvable
    kinds: Counter = field(default_factory=Counter)  # texte / OCR / mixte / blanc / docx / eml / sans repère
    pages: Counter = field(default_factory=Counter)  # text / ocr / vides / blanches
    text_ranges: Counter = field(default_factory=Counter)
    confidence_ranges: Counter = field(default_factory=Counter)
    short_text_documents: int = 0
    ocr_lines: int = 0
    flagged_lengths: Counter = field(default_factory=Counter)
    flagged_confidence: Counter = field(default_factory=Counter)
    flagged_places: Counter = field(default_factory=Counter)  # haut / milieu / bas
    flagged_without_letters: int = 0
    flagged_by_document: list[tuple[int, int]] = field(default_factory=list)  # (flagged, OCR lines)
    illegible_lines: int = 0
    illegible_documents: int = 0
    pages_per_document: list[int] = field(default_factory=list)


def measure(c: Corpus) -> Measures:
    m = Measures()
    for row in c.db.execute("SELECT original, status, parent FROM documents"):
        kind = PurePosixPath(row["original"]).suffix.lstrip(".") or "?"
        m.formats.setdefault(kind, Counter())[row["status"]] += 1
        m.attachments += row["parent"] is not None
        try:
            m.weights[kind] += (c.root / row["original"]).stat().st_size
        except OSError:
            m.missing_originals += 1
    for document in c.documents(DONE, REVIEW):
        if document.markdown is None:
            m.placement["introuvable"] += 1
            continue
        path = c.root / document.markdown
        m.placement["à ranger" if path.parent == c.inbox else "rangés"] += 1
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            m.placement["illisible"] += 1
            continue
        _measure_document(m, text)
    return m


def _measure_document(m: Measures, text: str) -> None:
    pages = pages_of(text)
    if not pages:
        unpaged = _UNPAGED.search(text)
        m.kinds[unpaged.group(1) if unpaged else "sans repère"] += 1
        return
    methods = {page.method for page in pages} - {"blank"}
    m.kinds["blanc" if not methods else "mixte" if len(methods) > 1 else "texte" if methods == {"text"} else "OCR"] += 1
    m.pages_per_document.append(len(pages))
    short = illegible = lines = 0
    for page in pages:
        if page.method == "blank" or not page.chars:
            m.pages["blanches" if page.method == "blank" else "vides"] += 1
            continue
        m.pages[page.method] += 1
        if page.method == "text":
            m.text_ranges[_range(page.chars, TEXT_RANGES)] += 1
            short += page.chars < SHORT_TEXT
        elif page.confidence is not None:
            m.confidence_ranges[_range(page.confidence, CONFIDENCE_RANGES)] += 1
        illegible += page.illegible
        lines += page.lines
        for flagged in page.flagged:
            m.flagged_lengths[_range(flagged.chars, LENGTH_RANGES)] += 1
            m.flagged_confidence[_range(flagged.confidence, FLAGGED_CONFIDENCE_RANGES)] += 1
            m.flagged_places["haut" if flagged.position <= EDGE else "bas" if flagged.position >= 1 - EDGE
                             else "milieu"] += 1
            m.flagged_without_letters += not flagged.letters
    m.ocr_lines += lines
    if illegible:
        m.flagged_by_document.append((illegible, lines))
    m.short_text_documents += short > 0
    m.illegible_lines += illegible
    m.illegible_documents += illegible > 0


def _range(value: float, ranges: list[tuple[float, str]]) -> str:
    return next(label for bound, label in ranges if value < bound)


def report(m: Measures) -> list[str]:
    statuses = [TODO, DONE, REVIEW, FAILED]
    total = sum(sum(counts.values()) for counts in m.formats.values())
    lines = [f"Documents : {total}" + (f" (dont {m.attachments} pièce(s) jointe(s) de mails)" if m.attachments else "")]
    if not total:
        return lines
    labels = [corpus.STATUS_LABELS[status] for status in statuses]
    lines.append("  " + "format".ljust(8) + "total".rjust(7) + "".join(label.rjust(12) for label in labels)
                 + "poids".rjust(11))
    for kind, counts in sorted(m.formats.items(), key=lambda item: -sum(item[1].values())):
        lines.append("  " + kind.ljust(8) + str(sum(counts.values())).rjust(7)
                     + "".join(str(counts[status]).rjust(12) for status in statuses)
                     + _megabytes(m.weights[kind]).rjust(11))
    lines.append("  originaux : " + _megabytes(sum(m.weights.values()))
                 + (f" · manquants : {m.missing_originals}" if m.missing_originals else ""))
    places = ["à ranger", "rangés", "introuvable", "illisible"]
    kinds = ["texte", "OCR", "mixte", "blanc", *sorted(extract.UNPAGED), "sans repère"]
    lines.append("Convertis : " + _join(m.placement, places, hide_zero=True))
    lines.append("  nature : " + _join(m.kinds, kinds, hide_zero=True))
    if m.pages_per_document:
        lines.append(f"Pages : {sum(m.pages.values())} · couche texte {m.pages['text']} · OCR {m.pages['ocr']}"
                     f" · vides {m.pages['vides']} · blanches {m.pages['blanches']}")
        lines.append(f"  par document : médiane {statistics.median(m.pages_per_document):g}"
                     f" · max {max(m.pages_per_document)}")
        lines.append("  couche texte, caractères par page : " + _join(m.text_ranges, [l for _, l in TEXT_RANGES]))
        lines.append(f"  pages texte de moins de {SHORT_TEXT} caractères : {m.text_ranges[TEXT_RANGES[0][1]]},"
                     f" dans {m.short_text_documents} document(s)")
        lines.append("  OCR, confiance minimale par page : "
                     + _join(m.confidence_ranges, [l for _, l in reversed(CONFIDENCE_RANGES)]))
        lines.append(f"  lignes « illisible ? » : {m.illegible_lines}, dans {m.illegible_documents} document(s)")
    if m.ocr_lines:
        lines += _flagged_report(m)
    return lines


def _flagged_report(m: Measures) -> list[str]:
    share = m.illegible_lines / m.ocr_lines
    lines = [f"Lignes OCR : {m.ocr_lines} · « illisible ? » {m.illegible_lines} ({_percent(share)})"]
    if not m.illegible_lines:
        return lines
    by_document = sorted(m.flagged_by_document, reverse=True)
    top = by_document[:3]
    shares = Counter(_range(flagged / total, SHARE_RANGES) for flagged, total in by_document)
    lines += [
        "  longueur (caractères) : " + _join(m.flagged_lengths, [label for _, label in LENGTH_RANGES]),
        f"  sans aucune lettre : {m.flagged_without_letters}",
        "  confiance : " + _join(m.flagged_confidence, [label for _, label in reversed(FLAGGED_CONFIDENCE_RANGES)]),
        f"  place dans la page (premiers et derniers {_percent(EDGE)} des lignes) : "
        + _join(m.flagged_places, ["haut", "milieu", "bas"]),
        "  part des lignes illisibles, par document touché : " + _join(shares, [label for _, label in SHARE_RANGES]),
        f"  les {len(top)} documents les plus touchés : {_percent(sum(f for f, _ in top) / m.illegible_lines)}"
        " des lignes illisibles ; dans chacun, " + " · ".join(_percent(f / t) for f, t in top) + " de ses lignes",
    ]
    return lines


def _percent(value: float) -> str:
    return f"{value:.0%}".replace("%", " %") if value >= 0.1 else f"{value:.1%}".replace(".", ",").replace("%", " %")


def _join(counts: Counter, order: list[str], hide_zero: bool = False) -> str:
    return " · ".join(f"{label} {counts[label]}" for label in order if counts[label] or not hide_zero) or "0"


def _megabytes(size: int) -> str:
    return f"{size / 1e6:.1f} Mo".replace(".", ",") if size >= 1e6 else f"{size / 1e3:.0f} Ko"


# --- checklist ----------------------------------------------------------------

_TICKED = re.compile(r"^\s*[-*+] \[[xX]\] .*<!-- id:([0-9a-f]{16}) -->\s*$", re.MULTILINE)
_LINK_BREAKERS = set("|#^[]")


def sync_checklist(c: Corpus) -> int:
    """Record the ticked boxes, then rewrite the list. Returns the documents newly checked."""
    path = c.root / CHECKLIST
    try:
        ticked = set(_TICKED.findall(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        ticked = set()
    checked = sum(c.mark_checked(prefix) for prefix in sorted(ticked))
    reviews, failures = c.documents(REVIEW), c.documents(FAILED)
    if reviews or failures or path.exists():
        corpus.write_atomic(path, render(reviews, failures))
    return checked


def render(reviews: list[Document], failures: list[Document]) -> str:
    lines = ["# À vérifier", "",
             "Paravent réécrit cette liste à chaque « paravent import » et « paravent etat » :"
             " n'y notez rien d'autre.", ""]
    if not reviews and not failures:
        lines.append("Rien à vérifier pour l'instant.")
    if reviews:
        lines += ["Comparez chaque document à son original, corrigez le Markdown si besoin, puis cochez sa case.", ""]
        for document in reviews:
            name = (_link(document.markdown, document.source_name) if document.markdown
                    else f"{document.source_name} (Markdown introuvable)")
            lines.append(f"- [ ] {name} · {_link(document.original, 'original')} — {_oneline(document.detail)}"
                         f" <!-- id:{document.sha256[:16]} -->")
    if failures:
        lines += ["", "## Conversions en échec", "",
                  "Relancez « paravent import --reessayer » ; si l'échec persiste, le document reste lisible"
                  " dans son original.", ""]
        lines += [f"- {_link(document.original, document.source_name)} — {_oneline(document.detail)}"
                  for document in failures]
    return "\n".join(lines) + "\n"


def _link(target: str, label: str) -> str:
    """A wikilink, or a Markdown link when a character would break the wikilink."""
    if _LINK_BREAKERS.isdisjoint(target + label):
        return f"[[{target.removesuffix('.md')}|{label}]]"
    return "[" + re.sub(r"([\\\[\]])", r"\\\1", label) + f"]({quote(target)})"


def _oneline(text: str | None) -> str:
    return " ".join((text or "").split())
