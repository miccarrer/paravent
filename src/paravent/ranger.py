"""Filing a converted document: where it goes in corpus/, and under which name.

The local AI, when configured, reads the document and suggests its type,
sender, date, a short title and a destination folder. The code keeps only
what it can check: a real ISO date, a folder that exists (anything else is
flagged as new), and a file name it builds itself from the date and title.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from . import ia

MAX_TEXT = 4000
MAX_FOLDERS = 200

SYSTEM = (
    "Tu aides à ranger des documents administratifs personnels (courriers, relevés, attestations, "
    "factures…). À partir du texte d'un document, tu donnes : son type (quelques mots), son émetteur "
    "(organisme ou personne qui l'envoie), sa date d'émission au format AAAA-MM-JJ (chaîne vide si "
    "absente), un titre court de 3 à 8 mots sans nom de personne, et le dossier où le ranger. "
    "Le dossier dépend d'abord de l'émetteur : si un dossier de la liste porte le nom de l'émetteur "
    "(ou son sigle), choisis celui-là, recopié exactement. Sinon, choisis le dossier de la liste qui "
    "correspond au sujet ; si aucun ne convient, propose un nouveau dossier court (« Organisme/Sujet »)."
)

SCHEMA = {
    "type": "object",
    "properties": {
        "type_document": {"type": "string"},
        "emetteur": {"type": "string"},
        "date": {"type": "string"},
        "titre": {"type": "string"},
        "dossier": {"type": "string"},
    },
    "required": ["type_document", "emetteur", "date", "titre", "dossier"],
}


@dataclass
class Proposal:
    document_type: str
    sender: str
    date: date | None
    title: str
    folder: str  # empty when the AI did not pick one
    new_folder: bool

    @property
    def name(self) -> str:
        return f"{self.date.isoformat()} {self.title}" if self.date else self.title


def propose(config: ia.Config, markdown: str, folders: list[str]) -> Proposal:
    listing = "\n".join(f"- {folder}" for folder in folders[:MAX_FOLDERS]) or "(aucun dossier pour l'instant)"
    prompt = f"Dossiers existants :\n{listing}\n\nTexte du document :\n{document_text(markdown)[:MAX_TEXT]}"
    answer = ia.ask(config, SYSTEM, prompt, SCHEMA)
    folder = answer["dossier"].strip().strip("/\\")
    known = {f.casefold(): f for f in folders}
    return Proposal(
        document_type=answer["type_document"].strip(),
        sender=answer["emetteur"].strip(),
        date=_parse_date(answer["date"]),
        title=answer["titre"].strip() or answer["type_document"].strip() or "document",
        folder=known.get(folder.casefold(), folder),
        new_folder=bool(folder) and folder.casefold() not in known,
    )


def document_text(markdown: str) -> str:
    """The document's text, without front matter and page markers."""
    text = re.sub(r"\A---\n.*?\n---\n", "", markdown, flags=re.DOTALL)
    text = re.sub(r"<!--.*?-->", "", text)
    text = re.sub(r"==(.*?)==", r"\1", text)  # doubtful OCR, highlighted
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _parse_date(value: str) -> date | None:
    try:
        parsed = date.fromisoformat(value.strip())
    except ValueError:
        return None
    # A letter dated in the far past or the future is a misreading, not a date.
    return parsed if 1950 <= parsed.year <= date.today().year + 1 else None


def inbox_files(inbox: Path) -> list[Path]:
    return sorted(inbox.glob("*.md"))
