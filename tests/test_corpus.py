import shutil
from pathlib import Path

import pytest

from paravent import corpus, extract

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def root(tmp_path):
    return corpus.create(tmp_path / "MonCorpus")


@pytest.fixture
def sources(tmp_path):
    folder = tmp_path / "sources"
    (folder / "sous-dossier").mkdir(parents=True)
    shutil.copy(FIXTURES / "convocation-native.pdf", folder)
    shutil.copy(FIXTURES / "courrier-scanne.pdf", folder / "sous-dossier")
    shutil.copy(FIXTURES / "courrier-scanne.pdf", folder / "z-copie.pdf")
    (folder / "notes.odt").write_bytes(b"x")
    (folder / ".cache").mkdir()
    (folder / ".cache" / "cache.pdf").write_bytes(b"%PDF")
    return folder


def test_create_lays_out_corpus(root):
    assert (root / "originaux").is_dir()
    assert (root / "corpus" / "_a-ranger").is_dir()
    assert corpus.find_root(root / "corpus" / "_a-ranger") == root


def test_create_refuses_non_empty_folder_and_existing_corpus(tmp_path, root):
    (tmp_path / "plein").mkdir()
    (tmp_path / "plein" / "fichier").write_text("x")
    with pytest.raises(corpus.CorpusError, match="pas vide"):
        corpus.create(tmp_path / "plein")
    with pytest.raises(corpus.CorpusError, match="déjà un corpus"):
        corpus.create(root)


def test_create_refuses_onedrive(tmp_path, monkeypatch):
    monkeypatch.setenv("OneDrive", str(tmp_path / "Nuage"))
    with pytest.raises(corpus.CorpusError, match="OneDrive"):
        corpus.create(tmp_path / "Nuage" / "Documents" / "MonCorpus")
    with pytest.raises(corpus.CorpusError, match="OneDrive"):
        corpus.create(tmp_path / "OneDrive - Famille" / "MonCorpus")
    assert corpus.create(tmp_path / "Nuage" / "MonCorpus", allow_onedrive=True)


def test_find_root_without_corpus(tmp_path):
    with pytest.raises(corpus.CorpusError):
        corpus.find_root(tmp_path)


def test_import_copies_deduplicates_and_converts(root, sources):
    with corpus.Corpus(root) as c:
        report = c.import_paths([sources])
        assert sorted(p.name for p in report.added) == ["convocation-native.pdf", "courrier-scanne.pdf"]
        assert [p.name for p in report.duplicates] == ["z-copie.pdf"]
        assert [p.name for p in report.unsupported] == ["notes.odt"]  # hidden .cache/ skipped
        assert report.converted == {"fait": 2, "a_verifier": 0, "echec": 0}
        assert c.counts()["fait"] == 2

        originals = sorted((root / "originaux").iterdir())
        assert len(originals) == 2
        markdown = (root / "corpus" / "_a-ranger" / "courrier-scanne.md").read_text(encoding="utf-8")
        assert markdown.startswith("---\noriginal: originaux/")
        assert 'nom_origine: "courrier-scanne.pdf"' in markdown
        assert "Lefèvre-Garçon" in markdown

        again = c.import_paths([sources])
        assert again.added == [] and len(again.duplicates) == 3


def test_interrupted_import_resumes_without_twins(root, sources, monkeypatch):
    calls = []

    def crash_on_second(path):
        calls.append(path)
        if len(calls) == 2:
            raise KeyboardInterrupt
        return [extract.Page(1, "texte", "text")]

    monkeypatch.setattr(extract, "extract_pages", crash_on_second)
    with corpus.Corpus(root) as c, pytest.raises(KeyboardInterrupt):
        c.import_paths([sources])
    with corpus.Corpus(root) as c:
        assert c.counts() == {"a_faire": 1, "fait": 1, "echec": 0, "a_verifier": 0}
        report = c.import_paths([])
        assert report.converted["fait"] == 1
        assert c.counts()["fait"] == 2
    assert len(list((root / "corpus" / "_a-ranger").glob("*.md"))) == 2


def test_failures_and_doubtful_pages_are_recorded(root, tmp_path, monkeypatch):
    for name in ("casse.pdf", "flou.pdf"):
        (tmp_path / name).write_bytes(name.encode())

    def fake(path):
        if path.stat().st_size == len(b"casse.pdf"):
            raise ValueError("PDF illisible")
        return [extract.Page(1, "net", "ocr", 0.99), extract.Page(2, "", "ocr", 0.0),
                extract.Page(3, "bof", "ocr", 0.55)]

    monkeypatch.setattr(extract, "extract_pages", fake)
    with corpus.Corpus(root) as c:
        c.import_paths([tmp_path / "casse.pdf", tmp_path / "flou.pdf"])
        failed, = c.documents(corpus.FAILED)
        assert failed.detail == "ValueError : PDF illisible"
        review, = c.documents(corpus.REVIEW)
        assert review.detail == "page 2 vide ou illisible ; page 3 : confiance OCR 0.55"

        monkeypatch.setattr(extract, "extract_pages", lambda path: [extract.Page(1, "réparé", "text")])
        assert c.import_paths([], retry_failed=True).converted["fait"] == 1
        assert c.documents(corpus.FAILED) == []


def test_move_files_a_document_safely(root, sources):
    with corpus.Corpus(root) as c:
        c.import_paths([sources / "convocation-native.pdf"])
        inbox_file = root / "corpus" / "_a-ranger" / "convocation-native.md"
        moved = c.move(inbox_file, "../France Travail//Convocations", 'Convocation: "entretien"?')
        assert moved == root / "corpus" / "France Travail" / "Convocations" / "Convocation entretien.md"
        assert c.documents()[0].markdown == "corpus/France Travail/Convocations/Convocation entretien.md"
        assert c.folders() == ["France Travail", "France Travail/Convocations"]

        with pytest.raises(corpus.CorpusError, match="boîte d'arrivée"):
            c.move(moved, "_a-ranger", "x")


def test_move_never_overwrites(root, sources):
    with corpus.Corpus(root) as c:
        c.import_paths([sources])
        first = c.move(root / "corpus" / "_a-ranger" / "convocation-native.md", "Divers", "Courrier")
        second = c.move(root / "corpus" / "_a-ranger" / "courrier-scanne.md", "Divers", "Courrier")
        assert (first.name, second.name) == ("Courrier.md", "Courrier (2).md")


@pytest.mark.parametrize(("name", "expected"), [
    ("  2026-03-12 Attestation CAF  ", "2026-03-12 Attestation CAF"),
    ("a/b\\c:d*e?f", "a b c d e f"),
    ("CON", "document CON"),
    ("...", "document"),
    ("x" * 200, "x" * 80),
])
def test_safe_filename(name, expected):
    assert corpus.safe_filename(name) == expected
