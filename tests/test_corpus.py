import os
import shutil
from pathlib import Path

import pytest

from paravent import corpus, extract, obsidian

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


def test_create_presets_obsidian_vault(root):
    import json

    app = root / ".obsidian" / "app.json"
    assert json.loads(app.read_text()) == obsidian.PRESET
    assert obsidian.warnings(root) == []
    app.write_text('{"showUnsupportedFiles": false, "strictLineBreaks": true}')
    obsidian.preset(root)  # user choices are kept, missing keys added
    settings = json.loads(app.read_text())
    assert settings["showUnsupportedFiles"] is False and settings["strictLineBreaks"] is True
    assert settings["newFileFolderPath"] == "corpus"


def test_create_turns_sync_and_publish_off(root):
    import json

    core = root / ".obsidian" / "core-plugins.json"
    assert json.loads(core.read_text()) == {"sync": False, "publish": False}
    core.write_text('{"file-explorer": true, "sync": true}')
    obsidian.preset(root)  # turned back on by the user: kept, and warned about
    assert json.loads(core.read_text()) == {"file-explorer": True, "sync": True, "publish": False}
    assert "aucun compte" in obsidian.warnings(root)[0]
    core.write_text('["file-explorer"]')  # older list format: left alone
    obsidian.preset(root)
    assert json.loads(core.read_text()) == ["file-explorer"]


def test_create_refuses_folder_inside_another_vault(tmp_path):
    (tmp_path / "Notes" / ".obsidian").mkdir(parents=True)
    with pytest.raises(corpus.CorpusError, match="coffre Obsidian"):
        corpus.create(tmp_path / "Notes" / "Papiers" / "MonCorpus")


def test_obsidian_warnings(root):
    config = root / ".obsidian"
    (config / "community-plugins.json").write_text('["paravent", "copilot"]')
    (config / "core-plugins.json").write_text('{"file-explorer": true, "sync": true, "publish": false}')
    found = obsidian.warnings(root)
    assert len(found) == 2
    assert "copilot" in found[0] and "paravent" not in found[0]
    assert "Obsidian Sync" in found[1]
    (config / "core-plugins.json").write_text('["publish"]')  # older list format
    assert "Obsidian Publish" in obsidian.warnings(root)[1]


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
        assert markdown.startswith('---\noriginal: "[[originaux/')
        assert 'nom_origine: "courrier-scanne.pdf"' in markdown
        assert "Lefèvre-Garçon" in markdown

        again = c.import_paths([sources])
        assert again.added == [] and len(again.duplicates) == 3


def test_email_attachments_become_documents(root):
    with corpus.Corpus(root) as c:
        report = c.import_paths([FIXTURES / "convocation.eml"])
        assert [p.name for p in report.added] == ["convocation.eml", "convocation.eml › convocation.pdf"]
        mail, attachment = c.documents()
        assert attachment.source_name == "convocation.pdf"
        text = (root / attachment.markdown).read_text(encoding="utf-8")
        assert f'piece_jointe_de: "[[{mail.original}]]"' in text
        assert "atelier collectif" in text

        again = c.import_paths([FIXTURES / "convocation.eml"])
        assert again.added == [] and len(again.duplicates) == 2


def test_interrupted_import_resumes_without_twins(root, sources, monkeypatch):
    calls = []

    def crash_on_second(path, on_page=None):
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

    def fake(path, on_page=None):
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

        monkeypatch.setattr(extract, "extract_pages", lambda path, on_page=None: [extract.Page(1, "réparé", "text")])
        assert c.import_paths([], retry_failed=True).converted["fait"] == 1
        assert c.documents(corpus.FAILED) == []


def test_blank_pages_are_not_doubtful_unless_the_whole_document_is(root, tmp_path, monkeypatch):
    (tmp_path / "verso.pdf").write_bytes(b"recto-verso")
    (tmp_path / "blanc.pdf").write_bytes(b"blanc")

    def fake(path, on_page=None):
        blank = extract.Page(2, "", "blank")
        return [extract.Page(1, "recto", "ocr", 0.97), blank] if path.stat().st_size == 11 else [blank]

    monkeypatch.setattr(extract, "extract_pages", fake)
    with corpus.Corpus(root) as c:
        c.import_paths([tmp_path / "verso.pdf", tmp_path / "blanc.pdf"])
        assert [(d.source_name, d.status, d.detail) for d in c.documents()] == [
            ("blanc.pdf", "a_verifier", "document entièrement blanc"), ("verso.pdf", "fait", None)]


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


def test_documents_are_found_again_after_a_manual_move(root, sources):
    with corpus.Corpus(root) as c:
        c.import_paths([sources / "convocation-native.pdf"])
        target = root / "corpus" / "Rangé à la main" / "convocation.md"
        target.parent.mkdir()
        (root / "corpus" / "_a-ranger" / "convocation-native.md").rename(target)
        document, = c.documents()
        assert document.markdown == "corpus/Rangé à la main/convocation.md"
        target.unlink()
        assert c.documents()[0].markdown is None


@pytest.mark.parametrize(("front", "expected"), [
    ('original: "[[originaux/ab.pdf]]"', "originaux/ab.pdf"),
    ("original: [[originaux/ab.pdf|scan]]", "originaux/ab.pdf"),
    ("original: originaux/ab.pdf", "originaux/ab.pdf"),
    ('original: "[[ailleurs/ab.pdf]]"', None),
    ("titre: x", None),
])
def test_original_of(tmp_path, front, expected):
    markdown = tmp_path / "doc.md"
    markdown.write_text(f"---\n{front}\n---\n\ntexte\n", encoding="utf-8")
    assert corpus.original_of(markdown) == expected


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


# --- reconversion -----------------------------------------------------------------

@pytest.fixture
def three_documents(root, tmp_path, monkeypatch):
    """Three documents converted by an « old » version; returns a function switching to the new one."""
    for number, name in enumerate(("a.pdf", "b.pdf", "c.pdf"), start=1):
        (tmp_path / name).write_bytes(b"x" * number)
    version = {"text": "ancienne conversion"}

    def fake(path, on_page=None):
        return [extract.Page(1, f"{version['text']} {path.stat().st_size}", "text")]

    monkeypatch.setattr(extract, "extract_pages", fake)
    with corpus.Corpus(root) as c:
        c.import_paths(sorted(tmp_path.glob("*.pdf")))
    return version


def markdown_of(root, name):
    return next(root.rglob(name)).read_text(encoding="utf-8")


def age(path, seconds):
    """As if the file had been written ``seconds`` after its conversion."""
    stamp = path.stat().st_mtime + seconds
    os.utime(path, (stamp, stamp))


def test_reconvert_rewrites_untouched_markdown_where_it_is_now(root, three_documents):
    inbox = root / "corpus" / "_a-ranger"
    (root / "corpus" / "Rangés").mkdir()
    (inbox / "a.md").rename(root / "corpus" / "Rangés" / "facture.md")  # moved in Obsidian: still untouched
    edited = inbox / "b.md"
    edited.write_text(edited.read_text(encoding="utf-8") + "\nMa note.\n", encoding="utf-8")
    age(edited, 60)
    (inbox / "c.md").unlink()
    three_documents["text"] = "nouvelle conversion"
    with corpus.Corpus(root) as c:
        trial = c.reconvert(dry_run=True)
        assert (trial.pending, [p.name for p in trial.edited], trial.missing) == (1, ["b.md"], 1)
        assert not (root / ".corpus" / "reconversions").exists()  # nothing written

        report = c.reconvert()
        assert report.converted == {"fait": 1, "a_verifier": 0} and report.failed == []
        assert "nouvelle conversion 1" in markdown_of(root, "facture.md")
        assert "Ma note." in markdown_of(root, "b.md") and "ancienne conversion" in markdown_of(root, "b.md")
        backup, = report.backup.glob("*.md")
        assert "ancienne conversion 1" in backup.read_text(encoding="utf-8")
        assert c.documents()[0].markdown == "corpus/Rangés/facture.md"
        assert (report.backup / "terminee").is_file()


def test_reconvert_keeps_ticks_and_survives_failures(root, three_documents, monkeypatch):
    with corpus.Corpus(root) as c:
        a, b, _ = c.documents()
        c.db.execute("UPDATE documents SET status = 'fait', detail = 'page 1 vide ou illisible'"
                     " WHERE sha256 = ?", (a.sha256,))  # ticked by the user
        c.db.commit()
        doubtful = [extract.Page(1, "", "ocr", 0.0)]
        monkeypatch.setattr(extract, "extract_pages", lambda path, on_page=None:
                            (_ for _ in ()).throw(ValueError("cassé")) if path.stat().st_size == 3 else doubtful)
        report = c.reconvert()
        assert report.converted == {"fait": 1, "a_verifier": 1} and len(report.failed) == 1
        statuses = {d.source_name: d.status for d in c.documents()}
        assert statuses == {"a.pdf": "fait", "b.pdf": "a_verifier", "c.pdf": "fait"}  # c: failed, state kept
        assert "ancienne conversion 3" in markdown_of(root, "c.md")


def test_interrupted_reconversion_resumes(root, three_documents, monkeypatch):
    calls = []

    def crash_on_second(path, on_page=None):
        calls.append(path)
        if len(calls) == 2:
            raise KeyboardInterrupt
        return [extract.Page(1, "nouvelle conversion", "text")]

    monkeypatch.setattr(extract, "extract_pages", crash_on_second)
    with corpus.Corpus(root) as c, pytest.raises(KeyboardInterrupt):
        c.reconvert()
    with corpus.Corpus(root) as c:
        report = c.reconvert()
        assert (report.already_done, report.pending) == (1, 2)
        assert len(calls) == 4
        assert len(list((root / ".corpus" / "reconversions").iterdir())) == 1  # the same run, resumed
        report = c.reconvert(dry_run=True)  # finished: a new run would redo them all
        assert (report.already_done, report.pending) == (0, 3)


def test_cli_reconvertir(root, three_documents, capsys):
    from paravent import cli

    three_documents["text"] = "nouvelle conversion"
    assert cli.main(["reconvertir", "--corpus", str(root), "--essai"]) == 0
    assert "À reconvertir : 3 · modifiés depuis leur conversion, laissés tels quels : 0" in capsys.readouterr().out
    assert cli.main(["reconvertir", "--corpus", str(root)]) == 0
    out = capsys.readouterr().out
    assert "Reconversion : 3 fait · 0 à vérifier · 0 en échec" in out and "Versions précédentes : .corpus/" in out
