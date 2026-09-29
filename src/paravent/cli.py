from __future__ import annotations

import argparse
import shutil
import sys
import time
from importlib.metadata import version
from pathlib import Path

from . import corpus, extract, ia, obsidian, ranger, review, update


def main(argv: list[str] | None = None) -> int:
    # Before anything else, --version included: a Paravent running during its own
    # update would lock the files being replaced.
    if update.in_progress():
        print("Paravent est en train de se mettre à jour. Réessayez dans une minute.", file=sys.stderr)
        return 75
    parser = argparse.ArgumentParser(prog="paravent", description="Documents personnels → corpus Markdown.")
    parser.add_argument("--version", action="version", version=version("paravent"))
    commands = parser.add_subparsers(dest="command", required=True)

    convert = commands.add_parser("convert", help="convertir un PDF ou une image en Markdown")
    convert.add_argument("source", type=Path)
    convert.add_argument("-o", "--output", type=Path, help="fichier Markdown à écrire (sinon : sortie standard)")

    init = commands.add_parser("init", help="créer un corpus dans un dossier vide")
    init.add_argument("dossier", type=Path)
    init.add_argument("--ignorer-onedrive", action="store_true",
                      help="créer le corpus même dans un dossier synchronisé par OneDrive")

    in_corpus = argparse.ArgumentParser(add_help=False)
    in_corpus.add_argument("--corpus", type=Path, default=Path.cwd(),
                           help="dossier du corpus (sinon : le dossier courant ou un de ses parents)")

    imp = commands.add_parser("import", parents=[in_corpus], help="importer des fichiers ou des dossiers")
    imp.add_argument("sources", type=Path, nargs="+")
    imp.add_argument("--reessayer", action="store_true", help="reconvertir aussi les documents en échec")

    commands.add_parser("etat", parents=[in_corpus], help="où en sont les documents importés")
    commands.add_parser("mesures", parents=[in_corpus],
                        help="chiffres sur la conversion, sans aucun nom : partageables tels quels")

    rng = commands.add_parser("ranger", parents=[in_corpus],
                              help="ranger les documents arrivés dans corpus/_a-ranger")
    rng.add_argument("--sans-ia", action="store_true", help="ne pas demander de proposition à l'IA locale")

    commands.add_parser("ia", help="état de l'IA locale facultative")

    upd = commands.add_parser("update", help="vérifier et installer une nouvelle version")
    upd.add_argument("--check", action="store_true", help="vérifier seulement, sans installer")
    upd.add_argument("--yes", action="store_true", help="installer sans demander de confirmation")

    args = parser.parse_args(argv)
    if (sys.stdout.encoding or "").lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        match args.command:
            case "convert":
                return _convert(args.source, args.output)
            case "init":
                return _init(args.dossier, args.ignorer_onedrive)
            case "import":
                return _import(args.corpus, args.sources, args.reessayer)
            case "etat":
                return _status(args.corpus)
            case "mesures":
                return _measures(args.corpus)
            case "ranger":
                return _file(args.corpus, use_ia=not args.sans_ia)
            case "ia":
                return _ia_status()
        return _update(check_only=args.check, assume_yes=args.yes)
    except (corpus.CorpusError, ia.IAError) as error:
        print(error, file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\nInterrompu. Relancez la même commande pour reprendre.", file=sys.stderr)
        return 130


def _convert(source: Path, output: Path | None) -> int:
    if not source.is_file():
        print(f"Fichier introuvable : {source}", file=sys.stderr)
        return 1
    try:
        markdown = extract.convert(source)
    except extract.UnsupportedFormat as error:
        print(error, file=sys.stderr)
        return 1
    if output is None:
        sys.stdout.write(markdown)
    else:
        output.write_text(markdown, encoding="utf-8")
    return 0


def _init(folder: Path, allow_onedrive: bool) -> int:
    root = corpus.create(folder, allow_onedrive=allow_onedrive)
    print(f"Corpus créé dans {root}")
    print(f"  {corpus.ORIGINALS}/  copies des documents importés (ne pas modifier)")
    print(f"  {corpus.DOCUMENTS}/     vos documents en Markdown ; les nouveaux arrivent dans {corpus.INBOX}/")
    print("Pour le parcourir avec Obsidian, ouvrez ce dossier comme coffre (sans y ajouter de plugins tiers).")
    return 0


def _import(where: Path, sources: list[Path], retry_failed: bool) -> int:
    with corpus.Corpus(corpus.find_root(where)) as c:
        for warning in obsidian.warnings(c.root):
            print(f"⚠ {warning}")
        progress = _ImportProgress()
        report = c.import_paths(sources, retry_failed=retry_failed, progress=progress)
        print(f"Nouveaux : {len(report.added)} · déjà dans le corpus : {len(report.duplicates)}"
              f" · formats non pris en charge : {len(report.unsupported)}")
        for path in report.unsupported:
            print(f"  ignoré : {path}")
        converted = report.converted
        print(f"Conversion : {converted[corpus.DONE]} fait · {converted[corpus.REVIEW]} à vérifier"
              f" · {converted[corpus.FAILED]} en échec" + progress.summary())
        _sync_checklist(c)
        return 1 if converted[corpus.FAILED] else 0


def _status(where: Path) -> int:
    with corpus.Corpus(corpus.find_root(where)) as c:
        _sync_checklist(c)
        counts = c.counts()
        print(" · ".join(f"{label} : {counts[status]}" for status, label in corpus.STATUS_LABELS.items()))
        for document in c.documents(corpus.REVIEW, corpus.FAILED):
            print(f"\n[{corpus.STATUS_LABELS[document.status]}] {document.source_name}")
            print(f"  {document.detail}")
            if document.status == corpus.REVIEW:
                print(f"  original : {document.original}\n  markdown : {document.markdown or 'introuvable'}")
        for warning in obsidian.warnings(c.root):
            print(f"\n⚠ {warning}")
        if counts[corpus.TODO]:
            print("\nDes documents restent à convertir : relancez « paravent import » pour reprendre.")
        waiting = ranger.inbox_files(c.inbox)
        if waiting:
            print(f"\n{len(waiting)} document(s) à ranger dans {corpus.DOCUMENTS}/{corpus.INBOX} (« paravent ranger »).")
    return 0


class _ImportProgress(corpus.Progress):
    """One line per document; in a terminal, redrawn page after page with a bar."""

    BAR = 20

    def __init__(self):
        self.live = sys.stdout.isatty()
        self.first = self.started = None
        self.pages = self.done = 0

    def document(self, number: int, total: int, name: str) -> None:
        self.number, self.total, self.name = number, total, name
        self.started = time.monotonic()
        self.first = self.first or self.started
        if not self.live:
            print(f"[{number}/{total}] {name}", flush=True)

    def page(self, done: int, total: int) -> None:
        self.pages = total
        if self.live:
            filled = self.BAR * done // max(total, 1)
            self._draw(f"{'█' * filled}{'░' * (self.BAR - filled)} page {min(done + 1, total)}/{total}")

    def converted(self, status: str) -> None:
        now = time.monotonic()
        self.done += 1
        line = f"{self.pages} p. · {_duration(now - self.started)}"
        if status != corpus.DONE:
            line += f" · {corpus.STATUS_LABELS[status]}"
        left = self.total - self.number
        if left and self.done >= 3:
            line += f" · reste ~{_duration((now - self.first) / self.done * left)}"
        if self.live:
            self._draw(line)
            print()
        else:
            print(f"  {line}", flush=True)

    def summary(self) -> str:
        return f" · en {_duration(time.monotonic() - self.first)}" if self.first else ""

    def _draw(self, tail: str) -> None:
        width = _terminal_width() - 1
        head = f"[{self.number}/{self.total}] "
        room = max(width - len(head) - len(tail) - 2, 8)
        name = self.name if len(self.name) <= room else self.name[:room - 1] + "…"
        print("\r" + f"{head}{name}  {tail}".ljust(width)[:width], end="", flush=True)


def _terminal_width() -> int:
    return shutil.get_terminal_size().columns


def _duration(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f} s"
    minutes = round(seconds / 60)
    return f"{minutes} min" if minutes < 90 else f"{minutes // 60} h {minutes % 60:02d}"


def _sync_checklist(c: corpus.Corpus) -> None:
    checked = review.sync_checklist(c)
    if checked:
        print(f"{checked} document(s) coché(s) dans « {review.CHECKLIST} » : passé(s) à « fait ».")
    remaining = c.counts()
    if remaining[corpus.REVIEW] or remaining[corpus.FAILED]:
        print(f"Liste à cocher dans Obsidian : « {review.CHECKLIST} », à la racine du corpus.")


def _measures(where: Path) -> int:
    with corpus.Corpus(corpus.find_root(where)) as c:
        print("\n".join(review.report(review.measure(c))))
    return 0


def _file(where: Path, use_ia: bool) -> int:
    config = ia.load_config() if use_ia else None
    with corpus.Corpus(corpus.find_root(where)) as c:
        waiting = ranger.inbox_files(c.inbox)
        if not waiting:
            print("Rien à ranger.")
            return 0
        if config:
            print(f"Propositions de l'IA locale ({config.model}). Elles restent des propositions.")
        for number, markdown in enumerate(waiting, start=1):
            print(f"\n[{number}/{len(waiting)}] {markdown.name}")
            proposal = None
            if config:
                try:
                    proposal = ranger.propose(config, markdown.read_text(encoding="utf-8"), c.folders())
                except ia.IAError as error:
                    print(f"  IA : {error}")
                    print("  L'IA est mise de côté jusqu'à la fin ; rangement à la main.")
                    config = None
            if proposal:
                when = proposal.date.isoformat() if proposal.date else "date non trouvée"
                print(f"  {proposal.document_type} · {proposal.sender} · {when}")
            if proposal and proposal.folder:
                print(f"  → {proposal.folder}/{corpus.safe_filename(proposal.name)}.md"
                      + ("   (nouveau dossier)" if proposal.new_folder else ""))
                choice = input("  [Entrée] accepter · a) autre destination · p) passer · q) quitter : ").strip().lower()
            else:
                choice = "a"
            if choice == "q":
                break
            if choice == "p":
                continue
            if choice == "a":
                folder = input("  Dossier (dans corpus/, vide = passer) : ").strip()
                if not folder:
                    continue
                default = corpus.safe_filename(proposal.name) if proposal else markdown.stem
                name = input(f"  Nom [{default}] : ").strip() or default
            elif choice == "" and proposal:
                folder, name = proposal.folder, proposal.name
            else:
                print("  Réponse non comprise : document laissé à ranger.")
                continue
            try:
                destination = c.move(markdown, folder, name)
            except corpus.CorpusError as error:
                print(f"  {error}")
                continue
            print(f"  rangé : {destination.relative_to(c.documents_dir).as_posix()}")
    return 0


def _ia_status() -> int:
    path = ia.config_path()
    config = ia.load_config(path)
    if config is None:
        print("Aucune IA locale configurée : Paravent fonctionne sans, simplement sans ses propositions.")
        print(f"Pour en utiliser une (Ollama), créez {path} :\n")
        print('[ia]\nurl = "http://localhost:11434"\nmodele = "gemma4:8b-16k"')
        return 0
    print(f"Configuration : {path}\nServeur : {config.url} · modèle : {config.model}")
    ia.check_local(config.url)
    models = ia.installed_models(config)
    if config.model not in models:
        print(f"Le modèle {config.model} n'est pas installé sur ce serveur. Modèles présents : {', '.join(models) or 'aucun'}",
              file=sys.stderr)
        return 1
    print("IA locale prête.")
    return 0


def _update(check_only: bool, assume_yes: bool) -> int:
    current = version("paravent")
    try:
        latest = update.fetch_latest()
        if not update.is_newer(latest.version, current):
            print(f"Paravent est à jour (version {current}).")
            return 0
        print(f"Nouvelle version disponible : {latest.version} (installée : {current}).")
        if check_only:
            return 0
        uv = update.find_uv()
        if not update.installed_as_uv_tool(uv):
            print("Cette copie de Paravent n'a pas été installée avec uv : mise à jour automatique impossible.",
                  file=sys.stderr)
            return 1
        if not assume_yes and input("Installer maintenant ? [o/N] ").strip().lower() not in {"o", "oui"}:
            print("Mise à jour annulée.")
            return 0
        wheel = update.download(latest, update.new_download_dir())
        log = update.install(wheel, uv)
    except update.UpdateError as error:
        print(error, file=sys.stderr)
        return 1
    if log is None:
        print(f"Paravent {latest.version} est installé.")
    else:
        print("La mise à jour s'installe. Relancez Paravent dans quelques secondes.")
        print(f"(journal : {log})")
    return 0
