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
