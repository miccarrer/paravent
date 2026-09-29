import importlib.util
import sys
from pathlib import Path

from paravent import corpus, extract

FIXTURES = Path(__file__).parent / "fixtures"
spec = importlib.util.spec_from_file_location("banc", Path(__file__).parents[1] / "scripts" / "banc.py")
banc = importlib.util.module_from_spec(spec)
sys.modules["banc"] = banc  # before running it: its dataclasses look their module up
spec.loader.exec_module(banc)


def flag(markdown, text, score):
    return markdown.replace(text, f"=={text}==<!-- illisible ? confiance {score:.2f} · ligne 1/10 -->", 1)


def test_line_check_end_to_end(tmp_path, capsys):
    root = corpus.create(tmp_path / "C")
    with corpus.Corpus(root) as c:
        c.import_paths([FIXTURES / "courrier-scanne.pdf"])
    markdown = root / "corpus" / "_a-ranger" / "courrier-scanne.md"
    text = markdown.read_text(encoding="utf-8")
    text = flag(text, "Objet : réexamen de vos droits à l'aide au logement", 0.62)
    text = flag(text, "Caisse d'Allocations Familiales de Exempleville", 0.41)
    text = flag(text, "court", 0.30)  # absent: not found in the original, skipped
    markdown.write_text(text, encoding="utf-8")

    assert banc.main(["lignes", "--corpus", str(root)]) == 0
    note = root / "_banc" / "Contrôle des lignes.md"
    content = note.read_text(encoding="utf-8")
    assert content.count("## Ligne") == 2 and len(list((root / "_banc" / "lignes").glob("*.png"))) == 2
    assert "> Objet : réexamen de vos droits à l'aide au logement" in content

    content = content.replace("- [ ] juste", "- [x] juste", 1)
    note.write_text(content, encoding="utf-8")
    assert banc.main(["lignes", "--corpus", str(root)]) == 1  # ticks are never overwritten
    capsys.readouterr()
    assert banc.main(["lignes", "--compter", "--corpus", str(root)]) == 0
    out = capsys.readouterr().out
    assert "Objet" not in out and "Caisse" not in out  # figures only
    rows = {line.split()[0]: line.split()[1:] for line in out.splitlines()[1:]}
    assert sum(map(int, rows["total"][:3])) == 1 and int(rows["total"][3]) == 1  # one ticked, one unanswered


def test_sample_spreads_over_ranges_and_documents():
    documents = [corpus.Document(f"{n:064x}", "x.pdf", "o.pdf", "m.md", "fait", None) for n in range(3)]
    lines = [banc.Line(documents[n % 3], 1, f"ligne numéro {n} assez longue", score)
             for n, score in enumerate([0.3, 0.35, 0.4, 0.6, 0.65, 0.75, 0.78, 0.45, 0.55, 0.72] * 3)]
    chosen = banc.sample(lines, 6, seed=1)
    assert len(chosen) == 6
    assert {line.range for line in chosen} == {"< 0,50", "0,50–0,70", "0,70–0,80"}
    assert max(sum(line.document is d for line in chosen) for d in documents) <= banc.PER_DOCUMENT


def test_references_are_prepared_then_compared(tmp_path, capsys):
    root = corpus.create(tmp_path / "C")
    with corpus.Corpus(root) as c:
        c.import_paths([FIXTURES / "courrier-scanne.pdf", FIXTURES / "paragraphes-natif.pdf"])
    assert banc.main(["preparer", "--corpus", str(root)]) == 0
    folder = root / "_banc" / "references"
    assert sorted(p.name for p in folder.iterdir()) == ["page-01.jpg", "page-01.md", "page-02.jpg", "page-02.md",
                                                       "pages.json"]
    assert banc.main(["preparer", "--corpus", str(root)]) == 1  # never over existing references
    assert banc.main(["preparer", "--corpus", str(root), "--ajouter"]) == 0  # nothing left to add
    capsys.readouterr()

    assert banc.main(["comparer", "--corpus", str(root), "--chaines", "actuel"]) == 1  # nothing ticked yet
    for note in folder.glob("page-*.md"):  # the conversion taken as the reference, with one word wrong
        text = note.read_text(encoding="utf-8").replace("- [ ] J'ai fini", "- [x] J'ai fini")
        note.write_text(text.replace("Exempleville", "Exempleville-sur-Mer", 1), encoding="utf-8")
    assert banc.main(["comparer", "--corpus", str(root), "--chaines", "actuel"]) == 0
    out = capsys.readouterr().out
    assert "Pages corrigées : 2 · texte 1 · OCR net 1" in out
    assert "Exempleville" not in out and "Caisse" not in out  # figures only
    row = next(line for line in out.splitlines() if line.startswith("actuel "))
    assert 0 < float(row.split()[1].replace(",", ".")) < 2  # a few characters wrong, no more


def test_metrics_ignore_markup_and_score_tables():
    reference = "## Relevé\n\n| Date | Montant |\n| 05/01 | 812,40 € |\n\nTotal : 812,40 €"
    candidate = "Relevé\n\n| Date | Montant |\n| --- | --- |\n| 05/01 | 812,40 |\n\n==Total : 812,40 €==<!-- x -->"
    assert banc.plain(reference) == "Relevé Date Montant 05/01 812,40 € Total : 812,40 €"
    assert banc.markdown_tables(candidate) == [[["Date", "Montant"], ["05/01", "812,40"]]]
    assert banc.table_score(banc.markdown_tables(reference), banc.markdown_tables(candidate)) == (1, 3, 4)
    assert banc.table_score(banc.markdown_tables(reference), []) == (0, 0, 4)
    characters, without_spaces, words = banc.errors(reference, candidate)
    assert characters == 2 / len(banc.plain(reference)) and words == 1 / 10  # « € » dropped in the table
    assert banc.errors("n° 1234567", "n°1234567")[1] == 0  # a missing space only


def test_blocks_are_read_row_by_row():
    label = ((100, 500, 400, 540), None, ["Net imposable"])
    amount = ((1200, 495, 1400, 545), "text", ["2 712,18 €"])  # its box starts a little higher
    title = ((100, 100, 900, 150), "paragraph_title", ["Relevé"])
    assert [item[2][0] for item in banc._reading_order([amount, label, title])] == [
        "Relevé", "Net imposable", "2 712,18 €"]
