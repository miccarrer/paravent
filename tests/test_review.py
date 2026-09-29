import shutil
from pathlib import Path

import pytest

from paravent import cli, corpus, extract, review

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def root(tmp_path):
    return corpus.create(tmp_path / "MonCorpus")


def fake_pages(tmp_path, monkeypatch, documents):
    """Import one empty source file per name; ``documents`` maps each name to its pages."""
    by_size = {}
    for number, (name, pages) in enumerate(documents.items(), start=1):
        (tmp_path / name).write_bytes(b"x" * number)
        by_size[number] = pages

    def fake(path, on_page=None):
        pages = by_size[path.stat().st_size]
        if isinstance(pages, Exception):
            raise pages
        return pages

    monkeypatch.setattr(extract, "extract_pages", fake)
    return [tmp_path / name for name in documents]


def test_flags_inside_rebuilt_paragraphs_are_read_back():
    markdown = extract.to_markdown([extract.Page(
        1, "début ==§==<!-- illisible ? confiance 0.41 · ligne 1/40 --> suite ==Montant dû==<!-- illisible ?"
           " confiance 0.66 · ligne 40/40 -->", "ocr", 0.41, lines=40)])
    page, = review.pages_of(markdown)
    assert (page.lines, page.chars) == (40, len("début § suite Montant dû"))
    assert page.flagged == [review.Flagged(1, 0.41, 0.0, False), review.Flagged(10, 0.66, 1.0, True)]


def test_noise_fragments_are_counted_apart():
    page = extract.Page(1, "texte net", "ocr", 0.97, lines=1, noise=[("§", 0.31), ("i:", 0.2)])
    empty = extract.Page(2, "", "ocr", 0.0, lines=0, noise=[("x", 0.1)])
    m = review.Measures()
    review._measure_document(m, extract.to_markdown([page, empty]))
    assert (m.noise_fragments, m.ocr_lines, m.illegible_lines, m.pages["vides"]) == (3, 1, 0, 1)
    assert "fragments mis à part (bruit probable) : 3" in "\n".join(review._flagged_report(m))


def test_pages_are_read_back_from_markers():
    markdown = extract.to_markdown([
        extract.Page(1, "x" * 150, "text"),
        extract.Page(2, "ligne\n\nfloue <!-- illisible ? confiance 0.61 -->", "ocr", 0.61),
        extract.Page(3, "", "ocr", 0.0),
    ])
    pages = review.pages_of("---\noriginal: x\n---\n\n" + markdown)
    assert pages == [review.Page("text", 150, None),
                     review.Page("ocr", len("ligne\n\nfloue"), 0.61, 2, [review.Flagged(5, 0.61, 1.0, True)]),
                     review.Page("ocr", 0, 0.0)]
    assert review.pages_of(extract.to_markdown([extract.Page(1, "bail", "docx")])) == []


def test_measures_of_real_conversions(root):
    with corpus.Corpus(root) as c:
        c.import_paths([FIXTURES])
        m = review.measure(c)
        assert {kind: sum(counts.values()) for kind, counts in m.formats.items()} == \
            {"pdf": 6, "png": 1, "docx": 1, "eml": 1}  # the e-mail's attachment is a PDF
        assert m.attachments == 1 and m.missing_originals == 0
        assert m.placement == {"à ranger": 9}
        assert m.kinds == {"texte": 3, "OCR": 3, "blanc": 1, "docx": 1, "eml": 1}
        assert m.pages == {"text": 3, "ocr": 3, "blanches": 1}
        assert (m.ocr_lines, m.illegible_lines) == (30, 0)  # counted from the page markers
        lines = review.report(m)
        assert lines[0] == "Documents : 9 (dont 1 pièce(s) jointe(s) de mails)"
        # Figures only: no name from the corpus shows up.
        names = ("convocation", "courrier", "paragraphes", "bail", "verso", ".pdf", "_a-ranger")
        assert not any(name in "\n".join(lines) for name in names)


def test_measures_flag_short_text_layers_and_illegible_lines(root, tmp_path, monkeypatch):
    sources = fake_pages(tmp_path, monkeypatch, {
        "tampon.pdf": [extract.Page(1, "Reçu le 12/03 — cachet de la mairie", "text"),
                       extract.Page(2, "x" * 1500, "text")],
        "mixte.pdf": [extract.Page(1, "x" * 500, "text"),
                      extract.Page(2, "a <!-- illisible ? confiance 0.50 -->\n\nb <!-- illisible ? confiance 0.70 -->",
                                   "ocr", 0.50),
                      extract.Page(3, "net", "ocr", 0.97),
                      extract.Page(4, "", "ocr", 0.0)],
        "casse.pdf": ValueError("PDF illisible"),
        "verso.pdf": [extract.Page(1, "x" * 1200, "text"), extract.Page(2, "", "blank")],
        "blanc.png": [extract.Page(1, "", "blank")],
    })
    with corpus.Corpus(root) as c:
        c.import_paths(sources)
        (root / "corpus" / "_a-ranger" / "tampon.md").rename(root / "corpus" / "tampon.md")
        m = review.measure(c)
    assert m.formats["pdf"] == {"fait": 2, "a_verifier": 1, "echec": 1}
    assert m.formats["png"] == {"a_verifier": 1}  # entirely blank
    assert m.placement == {"à ranger": 3, "rangés": 1}
    assert m.kinds == {"texte": 2, "mixte": 1, "blanc": 1}
    assert m.pages == {"text": 4, "ocr": 2, "vides": 1, "blanches": 2}
    assert m.text_ranges == {"< 200": 1, "200–999": 1, "≥ 1000": 2}
    assert m.confidence_ranges == {"< 0,80": 1, "≥ 0,95": 1}
    assert (m.short_text_documents, m.illegible_lines, m.illegible_documents) == (1, 2, 1)
    assert sorted(m.pages_per_document) == [1, 2, 2, 4]
    text = "\n".join(review.report(m))
    assert "pages texte de moins de 200 caractères : 1, dans 1 document(s)" in text
    assert "OCR, confiance minimale par page : ≥ 0,95 1 · 0,90–0,95 0 · 0,80–0,90 0 · < 0,80 1" in text


def flag(text, score):
    return f"{text} <!-- illisible ? confiance {score:.2f} -->"


def test_measures_describe_illegible_lines_without_their_text(root, tmp_path, monkeypatch):
    good = [f"ligne {n} bien lue" for n in range(4)]
    letter = [flag("§", 0.31), *good, flag("Montant dû au titre de l'année", 0.66), *good, flag("Jx", 0.75)]
    sources = fake_pages(tmp_path, monkeypatch, {
        "lettre.pdf": [extract.Page(1, "\n\n".join(letter), "ocr", 0.31)],
        "releve.pdf": [extract.Page(1, "\n\n".join([flag("12,50", 0.79)] + ["ok"] * 39), "ocr", 0.79)],
        "net.pdf": [extract.Page(1, "\n\n".join(["ok"] * 10), "ocr", 0.97)],
    })
    with corpus.Corpus(root) as c:
        c.import_paths(sources)
        m = review.measure(c)
    assert m.ocr_lines == 11 + 40 + 10 and m.illegible_lines == 4
    assert m.flagged_lengths == {"1–3": 2, "4–15": 1, "> 15": 1}
    assert m.flagged_without_letters == 2  # "§" and "12,50"
    assert m.flagged_confidence == {"< 0,50": 1, "0,50–0,70": 1, "0,70–0,80": 2}
    assert m.flagged_places == {"haut": 2, "milieu": 1, "bas": 1}
    assert sorted(m.flagged_by_document) == [(1, 40), (3, 11)]
    text = "\n".join(review.report(m))
    assert "Lignes OCR : 61 · « illisible ? » 4 (6,6 %)" in text
    assert "place dans la page (premiers et derniers 15 % des lignes) : haut 2 · milieu 1 · bas 1" in text
    assert "part des lignes illisibles, par document touché : < 5 % 1 · 5–20 % 0 · > 20 % 1" in text
    assert "les 2 documents les plus touchés : 100 % des lignes illisibles ; dans chacun, 27 % · 2,5 % de ses lignes" \
        in text
    assert "Montant" not in text and "Jx" not in text


def test_empty_corpus_measures(root):
    with corpus.Corpus(root) as c:
        assert review.report(review.measure(c)) == ["Documents : 0"]


def test_checklist_lists_doubts_and_failures_and_records_ticks(root, tmp_path, monkeypatch):
    sources = fake_pages(tmp_path, monkeypatch, {
        "flou.pdf": [extract.Page(1, "bof", "ocr", 0.55)],
        "vide.png": [extract.Page(1, "", "ocr", 0.0)],
        "casse.pdf": ValueError("PDF\nillisible"),
        "net.pdf": [extract.Page(1, "x" * 300, "text")],
    })
    checklist = root / review.CHECKLIST
    with corpus.Corpus(root) as c:
        assert review.sync_checklist(c) == 0 and not checklist.exists()  # nothing to list yet
        c.import_paths(sources)
        review.sync_checklist(c)
        flou, vide = c.documents(corpus.REVIEW)
        text = checklist.read_text(encoding="utf-8")
        assert (f"- [ ] [[corpus/_a-ranger/flou|flou.pdf]] · [[{flou.original}|original]]"
                f" — page 1 : confiance OCR 0.55 <!-- id:{flou.sha256[:16]} -->") in text
        assert "## Conversions en échec" in text and "— ValueError : PDF illisible" in text
        assert "net.pdf" not in text

        checklist.write_text(text.replace("- [ ] [[corpus/_a-ranger/flou", "- [x] [[corpus/_a-ranger/flou"),
                             encoding="utf-8")
        assert review.sync_checklist(c) == 1
        assert [d.source_name for d in c.documents(corpus.REVIEW)] == ["vide.png"]
        assert "flou.pdf" not in checklist.read_text(encoding="utf-8")

        c.mark_checked(vide.sha256[:16])
        monkeypatch.setattr(extract, "extract_pages", lambda path, on_page=None: [extract.Page(1, "réparé", "text")])
        c.import_paths([], retry_failed=True)
        review.sync_checklist(c)
        assert "Rien à vérifier pour l'instant." in checklist.read_text(encoding="utf-8")


def test_checklist_falls_back_to_markdown_links():
    document = corpus.Document("ab" * 32, "Facture #12 [copie].pdf", "originaux/abab.pdf",
                               "corpus/Factures/n°12 [copie].md", corpus.REVIEW, "page 1 vide ou illisible")
    line = review.render([document], []).splitlines()[-1]
    assert line.startswith(r"- [ ] [Facture #12 \[copie\].pdf](corpus/Factures/n%C2%B012%20%5Bcopie%5D.md)"
                           " · [[originaux/abab.pdf|original]]")


def test_import_names_are_safe_for_obsidian(root, tmp_path, monkeypatch):
    sources = fake_pages(tmp_path, monkeypatch, {"Facture #12 [copie].pdf": [extract.Page(1, "x" * 30, "text")]})
    with corpus.Corpus(root) as c:
        c.import_paths(sources)
        assert c.documents()[0].markdown == "corpus/_a-ranger/Facture 12 copie.md"


def test_cli_status_and_measures(root, capsys, monkeypatch):
    shutil.copy(FIXTURES / "courrier-scanne.pdf", root.parent)
    monkeypatch.setattr(extract, "extract_pages", lambda path, on_page=None: [extract.Page(1, "", "ocr", 0.0)])
    assert cli.main(["import", "--corpus", str(root), str(root.parent / "courrier-scanne.pdf")]) == 0
    assert "« À vérifier.md »" in capsys.readouterr().out
    text = (root / review.CHECKLIST).read_text(encoding="utf-8")
    (root / review.CHECKLIST).write_text(text.replace("- [ ]", "- [x]"), encoding="utf-8")
    assert cli.main(["etat", "--corpus", str(root)]) == 0
    assert "1 document(s) coché(s)" in capsys.readouterr().out
    assert cli.main(["mesures", "--corpus", str(root)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Documents : 1\n") and "courrier" not in out


def test_import_progress_without_terminal(root, capsys):
    shutil.copy(FIXTURES / "convocation-native.pdf", root.parent)
    assert cli.main(["import", "--corpus", str(root), str(root.parent / "convocation-native.pdf")]) == 0
    out = capsys.readouterr().out
    assert "[1/1] convocation-native.pdf\n  1 p. · 0 s\n" in out
    assert "Conversion : 1 fait · 0 à vérifier · 0 en échec · en 0 s" in out


def test_import_progress_in_terminal(capsys, monkeypatch):
    monkeypatch.setattr(cli, "_terminal_width", lambda: 60)  # not shutil's: pytest uses it too
    progress = cli._ImportProgress()
    progress.live = True
    progress.document(3, 12, "un nom de fichier bien trop long pour tenir sur la ligne.pdf")
    progress.page(1, 4)
    drawn = capsys.readouterr().out
    assert drawn.startswith("\r[3/12] un nom de fichier") and "…  █████░░░░░░░░░░░░░░░ page 2/4" in drawn
    assert len(drawn) == 1 + 59  # \r, then the terminal's width less one column
    progress.page(4, 4)
    progress.converted(corpus.REVIEW)
    assert "4 p. · 0 s · à vérifier" in capsys.readouterr().out


@pytest.mark.parametrize(("seconds", "shown"), [(4.4, "4 s"), (89, "89 s"), (150, "2 min"), (7300, "2 h 02")])
def test_durations(seconds, shown):
    assert cli._duration(seconds) == shown
