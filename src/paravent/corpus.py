"""The corpus: immutable originals, editable Markdown, and the import state.

Layout::

    MonCorpus/
    ├── originaux/   immutable copies, deduplicated by SHA-256
    ├── corpus/      Markdown, free-form tree; new documents land in _a-ranger/
    └── .corpus/     import state (SQLite) — never shared

An import runs in two phases, each committed file by file so that an
interrupted import resumes where it stopped: first every source file is
copied into ``originaux/`` and recorded as ``a_faire``; then each pending
document is converted. Its Markdown path is recorded *before* the file is
written, so a resumed conversion overwrites it instead of creating a twin.

Once written, a Markdown file belongs to the user, who may move or rename it
(in Obsidian, for instance). It is found again through its front matter,
``original: "[[originaux/<hash>.pdf]]"``, not through the recorded path.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import sqlite3
import stat
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import extract, obsidian

FORMAT_VERSION = 1
STATE_DIR = ".corpus"
DB_NAME = "paravent.db"
ORIGINALS = "originaux"
DOCUMENTS = "corpus"
INBOX = "_a-ranger"

TODO, DONE, FAILED, REVIEW = "a_faire", "fait", "echec", "a_verifier"
STATUS_LABELS = {TODO: "à faire", DONE: "fait", FAILED: "échec", REVIEW: "à vérifier"}
SUPPORTED_SUFFIXES = extract.SUPPORTED_SUFFIXES
ATTACHMENT_SEPARATOR = " › "

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE documents (
    sha256      TEXT PRIMARY KEY,
    source_name TEXT NOT NULL,
    source_path TEXT NOT NULL,
    original    TEXT NOT NULL,
    parent      TEXT REFERENCES documents (sha256),
    markdown    TEXT,
    status      TEXT NOT NULL CHECK (status IN ('a_faire', 'fait', 'echec', 'a_verifier')),
    detail      TEXT,
    imported_at TEXT NOT NULL,
    converted_at TEXT
);
"""


class CorpusError(RuntimeError):
    pass


@dataclass
class ImportReport:
    added: list[Path] = field(default_factory=list)
    duplicates: list[Path] = field(default_factory=list)
    unsupported: list[Path] = field(default_factory=list)
    converted: dict[str, int] = field(default_factory=lambda: {DONE: 0, REVIEW: 0, FAILED: 0})


@dataclass
class Document:
    sha256: str
    source_name: str
    original: str
    markdown: str | None
    status: str
    detail: str | None


class Corpus:
    def __init__(self, root: Path):
        self.root = root
        self.db = sqlite3.connect(root / STATE_DIR / DB_NAME)
        self.db.row_factory = sqlite3.Row
        version = self.db.execute("SELECT value FROM meta WHERE key = 'format'").fetchone()
        if version is None or int(version["value"]) != FORMAT_VERSION:
            raise CorpusError(f"Format de corpus inattendu dans {root} : {version and version['value']}")

    @property
    def documents_dir(self) -> Path:
        return self.root / DOCUMENTS

    @property
    def inbox(self) -> Path:
        return self.documents_dir / INBOX

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> Corpus:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- import -------------------------------------------------------------

    def import_paths(self, paths: Iterable[Path], retry_failed: bool = False,
                     progress: Callable[[str], None] = lambda message: None) -> ImportReport:
        report = ImportReport()
        for source in _walk(paths):
            if source.suffix.lower() not in SUPPORTED_SUFFIXES:
                report.unsupported.append(source)
            else:
                (report.added if self._register(source) else report.duplicates).append(source)
                if source.suffix.lower() == ".eml":
                    # Also for a known e-mail: an interrupted import may have missed its attachments.
                    self._register_attachments(source, report)
        if retry_failed:
            with self.db:
                self.db.execute("UPDATE documents SET status = ?, detail = NULL WHERE status = ?", (TODO, FAILED))
        pending = self.db.execute(
            "SELECT * FROM documents WHERE status = ? ORDER BY imported_at, source_name", (TODO,)).fetchall()
        for number, row in enumerate(pending, start=1):
            progress(f"[{number}/{len(pending)}] {row['source_name']}")
            report.converted[self._convert(row)] += 1
        return report

    def _register(self, source: Path) -> bool:
        """Copy a source file into originaux/ and record it; False if already known."""
        return self._record(_sha256(source), source.name, str(source.resolve()),
                            lambda partial: shutil.copyfile(source, partial))

    def _register_attachments(self, mail: Path, report: ImportReport) -> None:
        """Each attachment of an e-mail becomes a document of its own."""
        parent = _sha256(mail)
        for name, data in extract.mail_attachments(mail):
            name = safe_filename(Path(name).stem) + Path(name).suffix.lower()
            shown = Path(f"{mail}{ATTACHMENT_SEPARATOR}{name}")
            if Path(name).suffix not in SUPPORTED_SUFFIXES:
                report.unsupported.append(shown)
            elif self._record(hashlib.sha256(data).hexdigest(), name, str(shown.absolute()),
                              lambda partial: partial.write_bytes(data), parent=parent):
                report.added.append(shown)
            else:
                report.duplicates.append(shown)

    def _record(self, digest: str, name: str, source_path: str, write: Callable[[Path], object],
                parent: str | None = None) -> bool:
        if self.db.execute("SELECT 1 FROM documents WHERE sha256 = ?", (digest,)).fetchone():
            return False
        original = Path(ORIGINALS) / f"{digest[:16]}{Path(name).suffix.lower()}"
        target = self.root / original
        if not target.exists():
            partial = target.with_name(target.name + ".partiel")
            write(partial)
            os.replace(partial, target)
            target.chmod(stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
        with self.db:
            self.db.execute(
                "INSERT INTO documents (sha256, source_name, source_path, original, parent, status, imported_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (digest, name, source_path, original.as_posix(), parent, TODO, _now()))
        return True

    def _convert(self, row: sqlite3.Row) -> str:
        markdown = row["markdown"]
        if markdown is None:
            stem = safe_filename(Path(row["source_name"]).stem)
            markdown = self._free_name(self.inbox, stem).relative_to(self.root).as_posix()
            with self.db:
                self.db.execute("UPDATE documents SET markdown = ? WHERE sha256 = ?", (markdown, row["sha256"]))
        try:
            pages = extract.extract_pages(self.root / row["original"])
            body = extract.to_markdown(pages)
        except Exception as error:  # noqa: BLE001 — any failure is recorded, the import goes on
            status, detail = FAILED, f"{type(error).__name__} : {error}"
        else:
            status, detail = _review_status(pages)
            link = f"[[{row['original']}]]"  # clickable in Obsidian's Properties
            front = f"---\noriginal: {_yaml_str(link)}\nnom_origine: {_yaml_str(row['source_name'])}\n"
            if row["parent"]:
                mail, = self.db.execute("SELECT original FROM documents WHERE sha256 = ?", (row["parent"],)).fetchone()
                front += f"piece_jointe_de: {_yaml_str(f'[[{mail}]]')}\n"
            front += f"importe_le: {row['imported_at'][:10]}\n---\n\n"
            write_atomic(self.root / markdown, front + body)
        with self.db:
            self.db.execute("UPDATE documents SET status = ?, detail = ?, converted_at = ? WHERE sha256 = ?",
                            (status, detail, _now(), row["sha256"]))
        return status

    def mark_checked(self, prefix: str) -> int:
        """A doubtful document, compared with its original by the user, is done."""
        with self.db:
            return self.db.execute("UPDATE documents SET status = ? WHERE status = ? AND substr(sha256, 1, 16) = ?",
                                   (DONE, REVIEW, prefix)).rowcount

    # --- queries and moves --------------------------------------------------

    def counts(self) -> dict[str, int]:
        counts = dict.fromkeys(STATUS_LABELS, 0)
        for row in self.db.execute("SELECT status, count(*) AS n FROM documents GROUP BY status"):
            counts[row["status"]] = row["n"]
        return counts

    def documents(self, *statuses: str) -> list[Document]:
        """Documents with their Markdown's current location (None if it is gone)."""
        query = "SELECT * FROM documents"
        if statuses:
            query += f" WHERE status IN ({', '.join('?' * len(statuses))})"
        rows = self.db.execute(query + " ORDER BY imported_at, source_name", statuses).fetchall()
        located = self.locate()
        return [Document(r["sha256"], r["source_name"], r["original"],
                         located[r["original"]].relative_to(self.root).as_posix() if r["original"] in located else None,
                         r["status"], r["detail"])
                for r in rows]

    def locate(self) -> dict[str, Path]:
        """Map each original (``originaux/…``) to the Markdown file that points at it."""
        found = {}
        for markdown in sorted(self.documents_dir.rglob("*.md")):
            if original := original_of(markdown):
                found.setdefault(original, markdown)
        return found

    def folders(self) -> list[str]:
        """Existing folders under corpus/, relative, the inbox excluded."""
        return sorted(
            path.relative_to(self.documents_dir).as_posix()
            for path in self.documents_dir.rglob("*")
            if path.is_dir()
            and not any(part == INBOX or part.startswith(".") for part in path.relative_to(self.documents_dir).parts)
        )

    def move(self, markdown: Path, folder: str, name: str) -> Path:
        """Move a Markdown file to corpus/<folder>/<name>.md, never overwriting."""
        destination_dir = self.documents_dir / safe_folder(folder)
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = self._free_name(destination_dir, safe_filename(name))
        os.replace(markdown, destination)
        with self.db:
            self.db.execute("UPDATE documents SET markdown = ? WHERE markdown = ?",
                            (destination.relative_to(self.root).as_posix(),
                             markdown.relative_to(self.root).as_posix()))
        return destination

    def _free_name(self, folder: Path, stem: str) -> Path:
        taken = {row[0] for row in self.db.execute("SELECT markdown FROM documents WHERE markdown IS NOT NULL")}
        for number in range(1, 10_000):
            candidate = folder / (f"{stem}.md" if number == 1 else f"{stem} ({number}).md")
            if not candidate.exists() and candidate.relative_to(self.root).as_posix() not in taken:
                return candidate
        raise CorpusError(f"Trop de fichiers nommés « {stem} » dans {folder}")


# --- creating and finding a corpus ------------------------------------------

def create(root: Path, allow_onedrive: bool = False) -> Path:
    root = root.expanduser().resolve()
    if not allow_onedrive and (cloud := onedrive_folder(root)):
        raise CorpusError(
            f"Ce dossier est synchronisé par OneDrive ({cloud}) : tous les documents partiraient en clair "
            "dans le cloud. Choisissez un dossier hors de OneDrive.")
    if vault := obsidian.enclosing_vault(root):
        raise CorpusError(
            f"Ce dossier est à l'intérieur du coffre Obsidian {vault} : ses plugins et tout ce qui le lit "
            "verraient vos documents. Créez le corpus ailleurs ; il sera son propre coffre.")
    if (root / STATE_DIR).exists():
        raise CorpusError(f"Il y a déjà un corpus dans {root}")
    if root.exists() and any(root.iterdir()):
        raise CorpusError(f"Le dossier {root} n'est pas vide.")
    for folder in (ORIGINALS, f"{DOCUMENTS}/{INBOX}", STATE_DIR):
        (root / folder).mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(root / STATE_DIR / DB_NAME)
    with db:
        db.executescript(SCHEMA)
        db.execute("INSERT INTO meta VALUES ('format', ?)", (str(FORMAT_VERSION),))
    db.close()
    obsidian.preset(root)
    return root


def find_root(start: Path) -> Path:
    start = start.expanduser().resolve()
    for folder in (start, *start.parents):
        if (folder / STATE_DIR / DB_NAME).is_file():
            return folder
    raise CorpusError(f"Aucun corpus trouvé dans {start} ni au-dessus (créez-en un avec « paravent init »).")


def onedrive_folder(path: Path) -> Path | None:
    """Return the OneDrive folder that contains ``path``, if any."""
    roots = [Path(value) for name, value in os.environ.items() if name.upper().startswith("ONEDRIVE") and value]
    for root in roots:
        if path.is_relative_to(root.expanduser().resolve()):
            return root
    for parent in (path, *path.parents):
        if parent.name.lower().startswith("onedrive"):
            return parent
    return None


_FRONT_MATTER = re.compile(r"\A---\r?\n(.*?)\r?\n---", re.DOTALL)
_ORIGINAL = re.compile(r"^original:\s*(.+?)\s*$", re.MULTILINE)


def original_of(markdown: Path) -> str | None:
    """The ``originaux/…`` path a Markdown file's front matter points at."""
    try:
        with open(markdown, encoding="utf-8") as file:
            head = file.read(4096)
    except (OSError, UnicodeDecodeError):
        return None
    front = _FRONT_MATTER.match(head)
    value = front and _ORIGINAL.search(front.group(1))
    if not value:
        return None
    target = value.group(1).strip("'\"").removeprefix("[[").removesuffix("]]").split("|")[0].strip()
    return target if target.startswith(f"{ORIGINALS}/") else None


# --- names ------------------------------------------------------------------

# Windows forbids the first ones; Obsidian also refuses # ^ [ ] in a note's name (they break links).
_FORBIDDEN = re.compile(r'[<>:"/\\|?*#^\[\]\x00-\x1f]')
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
MAX_NAME = 80


def safe_filename(name: str) -> str:
    """A file stem valid on Windows and Linux (without the .md suffix)."""
    name = _FORBIDDEN.sub(" ", name)
    name = re.sub(r"\s+", " ", name).strip(" .")[:MAX_NAME].strip(" .")
    if not name or name.lower() in _RESERVED:
        name = f"document {name}".strip()
    return name


def safe_folder(folder: str) -> Path:
    """A relative folder under corpus/: no absolute path, no '..', no inbox."""
    parts = [safe_filename(part) for part in re.split(r"[/\\]+", folder) if part.strip(" .")]
    if not parts:
        raise CorpusError("Dossier de destination vide.")
    if parts[0] == INBOX:
        raise CorpusError(f"« {INBOX} » est la boîte d'arrivée, pas un dossier de rangement.")
    return Path(*parts)


# --- helpers ------------------------------------------------------------------

def _walk(paths: Iterable[Path]) -> Iterator[Path]:
    for path in paths:
        if path.is_dir():
            yield from sorted(p for p in path.rglob("*")
                              if p.is_file() and not any(part.startswith(".") for part in p.relative_to(path).parts))
        elif path.is_file():
            yield path
        else:
            raise CorpusError(f"Introuvable : {path}")


def _review_status(pages: list[extract.Page]) -> tuple[str, str | None]:
    if pages and all(page.method == "blank" for page in pages):
        return REVIEW, "document entièrement blanc"
    reasons = []
    for page in pages:
        if page.method == "blank":
            continue
        if not page.text:
            reasons.append(f"page {page.number} vide ou illisible")
        elif page.min_confidence is not None and page.min_confidence < extract.LOW_CONFIDENCE:
            reasons.append(f"page {page.number} : confiance OCR {page.min_confidence:.2f}")
    return (REVIEW, " ; ".join(reasons)) if reasons else (DONE, None)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        while chunk := file.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".partiel")
    partial.write_text(text, encoding="utf-8", newline="\n")
    os.replace(partial, path)


def _yaml_str(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")

