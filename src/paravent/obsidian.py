"""The corpus folder opened as an Obsidian vault.

``MonCorpus/`` is the vault: ``.corpus/`` stays hidden (Obsidian skips dot
folders), ``originaux/`` shows up in its PDF viewer, and the mirror lives
outside. Opening the vault is optional; this module only presets it and
warns about what would send the documents elsewhere: third-party plugins
(they read the whole vault once restricted mode is off), Sync and Publish,
or a corpus nested inside a larger vault.
"""

from __future__ import annotations

import json
from pathlib import Path

CONFIG_DIR = ".obsidian"
OUR_PLUGIN = "paravent"
LEAKY_CORE_PLUGINS = {"sync": "Obsidian Sync", "publish": "Obsidian Publish"}


PRESET = {
    # New notes go into corpus/ rather than the vault root (outside the corpus).
    "newFileLocation": "folder",
    "newFileFolderPath": "corpus",
    # Show every original (.docx, .eml…), not only the types Obsidian displays;
    # a link to a hidden one would otherwise look unresolved.
    "showUnsupportedFiles": True,
}


def preset(root: Path) -> None:
    """Add the settings Paravent needs, keeping any the user has already chosen."""
    config = root / CONFIG_DIR
    config.mkdir(exist_ok=True)
    app = config / "app.json"
    current = _read_json(app, {})
    if not isinstance(current, dict):
        return
    merged = PRESET | current
    if merged != current:
        app.write_text(json.dumps(merged, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def enclosing_vault(path: Path) -> Path | None:
    """A larger Obsidian vault that contains ``path``, if any."""
    for parent in path.parents:
        if (parent / CONFIG_DIR).is_dir():
            return parent
    return None


def warnings(root: Path) -> list[str]:
    found = []
    if vault := enclosing_vault(root):
        found.append(f"Le corpus est à l'intérieur du coffre Obsidian {vault} : tout ce qui lit ce coffre "
                     "(plugins, synchronisation, IA) lit aussi vos documents.")
    config = root / CONFIG_DIR
    plugins = [name for name in _read_json(config / "community-plugins.json", []) if name != OUR_PLUGIN]
    if plugins:
        found.append("Plugins Obsidian tiers actifs dans ce coffre : " + ", ".join(plugins)
                     + ". Ils peuvent lire tous les documents ; désactivez-les pour ce coffre.")
    core = _read_json(config / "core-plugins.json", {})
    enabled = {name for name, on in core.items() if on} if isinstance(core, dict) else set(core)
    for name, label in LEAKY_CORE_PLUGINS.items():
        if name in enabled:
            found.append(f"{label} est activé dans ce coffre : les documents quittent cet ordinateur.")
    return found


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default
