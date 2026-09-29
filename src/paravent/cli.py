from __future__ import annotations

import argparse
import sys
from importlib.metadata import version
from pathlib import Path

from . import extract, update


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="paravent", description="Documents personnels → corpus Markdown.")
    parser.add_argument("--version", action="version", version=version("paravent"))
    commands = parser.add_subparsers(dest="command", required=True)

    convert = commands.add_parser("convert", help="convertir un PDF ou une image en Markdown")
    convert.add_argument("source", type=Path)
    convert.add_argument("-o", "--output", type=Path, help="fichier Markdown à écrire (sinon : sortie standard)")

    upd = commands.add_parser("update", help="vérifier et installer une nouvelle version")
    upd.add_argument("--check", action="store_true", help="vérifier seulement, sans installer")
    upd.add_argument("--yes", action="store_true", help="installer sans demander de confirmation")

    args = parser.parse_args(argv)
    if args.command == "convert":
        return _convert(args.source, args.output)
    return _update(check_only=args.check, assume_yes=args.yes)


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
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdout.write(markdown)
    else:
        output.write_text(markdown, encoding="utf-8")
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
