import difflib
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from paravent import extract

FIXTURES = Path(__file__).parent / "fixtures"

# Same text as make_fixtures.LETTER, kept here so the tests do not import the generator.
LETTER = """Caisse d'Allocations Familiales de Exempleville
Madame Élodie Lefèvre-Garçon
12, rue des Écoles — 99000 Exempleville
Objet : réexamen de vos droits à l'aide au logement
Madame, nous accusons réception de votre dossier n° 1234567 A.
Vos prestations d'août sont maintenues à 312,45 € par mois.
Tél. : 01 23 45 67 89 — courriel : elodie.lefevre@exemple.fr
Numéro de sécurité sociale : 2 84 05 99 123 456 78
IBAN : FR76 3000 6000 0112 3456 7890 189
Veuillez agréer, Madame, l'expression de nos salutations distinguées."""


def squash(text: str) -> str:
    # OCR drops or adds spaces inside numbers: compare without whitespace.
    return "".join(text.split())


def test_native_pdf_uses_text_layer():
    pages = extract.extract_pages(FIXTURES / "convocation-native.pdf")
    assert [page.method for page in pages] == ["text"]
    assert "Convocation à un entretien le 12 février à 9 h 30." in pages[0].text


def test_scanned_pdf_goes_through_ocr_and_keeps_accents():
    pages = extract.extract_pages(FIXTURES / "courrier-scanne.pdf")
    assert [page.method for page in pages] == ["ocr"]
    text = pages[0].text
    for expected in ("Élodie Lefèvre-Garçon", "réexamen", "312,45 €", "salutations distinguées"):
        assert expected in text
    similarity = difflib.SequenceMatcher(None, squash(LETTER), squash(text)).ratio()
    assert similarity > 0.95, similarity


def test_image_goes_through_ocr():
    pages = extract.extract_pages(FIXTURES / "courrier-photo.png")
    assert pages[0].method == "ocr"
    assert "Lefèvre-Garçon" in pages[0].text


def test_markdown_marks_pages():
    markdown = extract.convert(FIXTURES / "convocation-native.pdf")
    assert markdown.startswith("<!-- page 1 · text -->\n\n")


def test_low_confidence_lines_are_flagged(monkeypatch):
    fake = SimpleNamespace(txts=("net", "flou"), scores=(0.99, 0.41))
    monkeypatch.setattr(extract, "_ocr_engine", lambda: lambda image: fake)
    page = extract._ocr_page(1, Image.new("RGB", (10, 10)))
    assert page.text == "net\n\nflou <!-- illisible ? confiance 0.41 -->"
    assert page.min_confidence == 0.41


def test_blank_page_is_reported_not_invented(monkeypatch):
    monkeypatch.setattr(extract, "_ocr_engine", lambda: lambda image: SimpleNamespace(txts=None, scores=None))
    page = extract._ocr_page(3, Image.new("RGB", (10, 10)))
    assert "page vide ou illisible" in extract.to_markdown([page])


def test_unsupported_format(tmp_path):
    source = tmp_path / "notes.odt"
    source.write_bytes(b"")
    with pytest.raises(extract.UnsupportedFormat):
        extract.extract_pages(source)
