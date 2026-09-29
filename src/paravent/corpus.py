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

from . import extract

FORMAT_VERSION = 1
STATE_DIR = ".corpus"
DB_NAME = "paravent.db"
ORIGINALS = "originaux"
DOCUMENTS = "corpus"
INBOX = "_a-ranger"

TODO, DONE, FAILED, REVIEW = "a_faire", "fait", "echec", "a_verifier"
STATUS_LABELS = {TODO: "à faire", DONE: "fait", FAILED: "échec", REVIEW: "à vérifier"}
SUPPORTED_SUFFIXES = {".pdf"} | extract.IMAGE_SUFFIXES

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE documents (
    sha256      TEXT PRIMARY KEY,
    source_name TEXT NOT NULL,
    source_path TEXT NOT NULL,
    original    TEXT NOT NULL,
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
            elif self._register(source):
                report.added.append(source)
            else:
                report.duplicates.append(source)
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
        digest = _sha256(source)
        if self.db.execute("SELECT 1 FROM documents WHERE sha256 = ?", (digest,)).fetchone():
            return False
        original = Path(ORIGINALS) / f"{digest[:16]}{source.suffix.lower()}"
        target = self.root / original
        if not target.exists():
            partial = target.with_name(target.name + ".partiel")
            shutil.copyfile(source, partial)
            os.replace(partial, target)
            target.chmod(stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
        with self.db:
            self.db.execute(
                "INSERT INTO documents (sha256, source_name, source_path, original, status, imported_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (digest, source.name, str(source.resolve()), original.as_posix(), TODO, _now()))
        return True

    def _convert(self, row: sqlite3.Row) -> str:
        markdown = row["markdown"]
        if markdown is None:
            markdown = self._free_name(self.inbox, Path(row["source_name"]).stem).relative_to(self.root).as_posix()
            with self.db:
                self.db.execute("UPDATE documents SET markdown = ? WHERE sha256 = ?", (markdown, row["sha256"]))
        try:
            pages = extract.extract_pages(self.root / row["original"])
            body = extract.to_markdown(pages)
        except Exception as error:  # noqa: BLE001 — any failure is recorded, the import goes on
            status, detail = FAILED, f"{type(error).__name__} : {error}"
        else:
            status, detail = _review_status(pages)
            front = (f"---\noriginal: {row['original']}\nnom_origine: {_yaml_str(row['source_name'])}\n"
                     f"importe_le: {row['imported_at'][:10]}\n---\n\n")
            _write_atomic(self.root / markdown, front + body)
        with self.db:
            self.db.execute("UPDATE documents SET status = ?, detail = ?, converted_at = ? WHERE sha256 = ?",
                            (status, detail, _now(), row["sha256"]))
        return status

    # --- queries and moves --------------------------------------------------

    def counts(self) -> dict[str, int]:
        counts = dict.fromkeys(STATUS_LABELS, 0)
        for row in self.db.execute("SELECT status, count(*) AS n FROM documents GROUP BY status"):
            counts[row["status"]] = row["n"]
        return counts

    def documents(self, *statuses: str) -> list[Document]:
        query = "SELECT * FROM documents"
        if statuses:
            query += f" WHERE status IN ({', '.join('?' * len(statuses))})"
        rows = self.db.execute(query + " ORDER BY imported_at, source_name", statuses)
        return [Document(r["sha256"], r["source_name"], r["original"], r["markdown"], r["status"], r["detail"])
                for r in rows]

    def folders(self) -> list[str]:
        """Existing folders under corpus/, relative, the inbox excluded."""
        return sorted(
            path.relative_to(self.documents_dir).as_posix()
            for path in self.documents_dir.rglob("*")
            if path.is_dir() and INBOX not in path.relative_to(self.documents_dir).parts
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


# --- names ------------------------------------------------------------------

_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
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
    reasons = []
    for page in pages:
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


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".partiel")
    partial.write_text(text, encoding="utf-8", newline="\n")
    os.replace(partial, path)


def _yaml_str(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")

