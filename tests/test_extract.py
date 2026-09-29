import difflib
import math
from pathlib import Path
from types import SimpleNamespace

import pypdfium2 as pdfium
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


def a4_page(draw=lambda d: None, paper=250) -> Image.Image:
    """An A4 page at 200 dpi, softened like a scan."""
    from PIL import ImageDraw, ImageFilter

    image = Image.new("L", (1654, 2339), paper)
    draw(ImageDraw.Draw(image))
    return image.filter(ImageFilter.GaussianBlur(0.8))


def signature(draw, grey=30):
    # 15 mm wide, 2 px pen: a small signature.
    draw.line([(900 + t * 1.1, 1900 + 20 * math.sin(t / 9) + 12 * math.sin(t / 3.1)) for t in range(140)],
              fill=grey, width=2)


def handwritten_lu(draw, grey=40):
    draw.line([(300, 1500), (300, 1560), (335, 1560)], fill=grey, width=2)
    draw.line([(350, 1530), (352, 1558), (375, 1556), (378, 1530), (380, 1560)], fill=grey, width=2)


def scanned_verso() -> Image.Image:
    pdf = pdfium.PdfDocument(FIXTURES / "verso-blanc.pdf")
    return pdf[0].render(scale=extract.OCR_DPI / 72).to_pil()


@pytest.mark.parametrize(("name", "page", "blank"), [
    ("page blanche", lambda: a4_page(), True),
    ("verso scanné : poussières, recto en transparence, bord, perforations", scanned_verso, True),
    ("petite signature", lambda: a4_page(signature), False),
    ("signature à l'encre claire", lambda: a4_page(lambda d: signature(d, grey=140)), False),
    ("« Lu » manuscrit", lambda: a4_page(handwritten_lu), False),
    ("« Lu » au crayon", lambda: a4_page(lambda d: handwritten_lu(d, grey=150)), False),
    ("photo sombre", lambda: a4_page(paper=120), False),
])
def test_is_blank(name, page, blank):
    assert extract.is_blank(page()) is blank


def test_blank_verso_is_not_a_doubtful_page(tmp_path):
    recto_verso = pdfium.PdfDocument.new()
    for source in ("courrier-scanne.pdf", "verso-blanc.pdf"):
        recto_verso.import_pages(pdfium.PdfDocument(FIXTURES / source))
    recto_verso.save(tmp_path / "recto-verso.pdf")
    pages = extract.extract_pages(tmp_path / "recto-verso.pdf")
    assert [page.method for page in pages] == ["ocr", "blank"]
    assert extract.to_markdown(pages).endswith("<!-- page 2 · blank -->\n\n<!-- page blanche -->\n")


def test_unsupported_format(tmp_path):
    source = tmp_path / "notes.odt"
    source.write_bytes(b"")
    with pytest.raises(extract.UnsupportedFormat):
        extract.extract_pages(source)


def test_word_document_keeps_structure():
    markdown = extract.convert(FIXTURES / "bail.docx")
    assert markdown.startswith("<!-- docx -->\n\n<!-- en-tête -->\nAgence Exemple Immobilier")
    assert "# Bail d'habitation" in markdown and "## Loyer" in markdown
    assert "- Monsieur Gérard Exemplaire, bailleur\n- Madame Élodie Lefèvre-Garçon, locataire" in markdown
    assert "Le loyer mensuel est fixé à 540,00 €, charges comprises." in markdown
    assert "| Poste | Montant |\n| --- | --- |\n| Loyer | 480,00 € |" in markdown


def test_email_headers_body_and_attachment_names():
    markdown = extract.convert(FIXTURES / "convocation.eml")
    assert "**De :** Conseillère Pôle Exemple <conseil@pole-exemple.fr>" in markdown
    assert "**Date :** 2026-02-02 10:15" in markdown
    assert "**Objet :** Votre convocation du 12 février" in markdown
    assert "Vous trouverez ci-joint votre convocation." in markdown
    assert "<p>" not in markdown  # the plain-text alternative wins
    assert markdown.rstrip().endswith("**Pièces jointes :** convocation.pdf")


def test_email_attachments():
    (name, data), = extract.mail_attachments(FIXTURES / "convocation.eml")
    assert name == "convocation.pdf" and data.startswith(b"%PDF")


def test_html_only_email(tmp_path):
    from email.message import EmailMessage

    message = EmailMessage()
    message["Subject"] = "Relevé"
    message.set_content("<html><head><style>p{}</style></head><body><p>Solde&nbsp;: 12,00&nbsp;€</p>"
                        "<ul><li>un</li><li>deux</li></ul><script>x()</script></body></html>", subtype="html")
    source = tmp_path / "releve.eml"
    source.write_bytes(bytes(message))
    markdown = extract.convert(source)
    assert "Solde : 12,00 €" in markdown
    assert "- un\n- deux" in markdown
    assert "x()" not in markdown and "p{}" not in markdown
