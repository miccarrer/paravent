"""End-to-end check of the install → convert → self-update loop.

Builds two wheels (0.0.1 and 0.0.2) from this checkout, installs 0.0.1 as a
uv tool, converts a scanned fixture, then runs ``paravent update`` against a
local release feed and waits until 0.0.2 answers. Everything happens in a
temporary directory: the uv tools of the machine are left untouched.

    uv run python scripts/ci_update_scenario.py
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHON = "3.12"
OLD, NEW = "0.0.1", "0.0.2"
UPDATE_TIMEOUT = 240


def run(command: list, env: dict, **kwargs) -> subprocess.CompletedProcess:
    print("$", " ".join(str(part) for part in command), flush=True)
    return subprocess.run([str(part) for part in command], env=env, text=True, encoding="utf-8", **kwargs)


def build_wheel(version: str, work: Path, env: dict) -> Path:
    source = work / f"src-{version}"
    shutil.copytree(ROOT, source, ignore=shutil.ignore_patterns(".git", ".venv", "dist", "__pycache__", ".pytest_cache", ".context"))
    pyproject = source / "pyproject.toml"
    pyproject.write_text(re.sub(r'(?m)^version = ".*"$', f'version = "{version}"', pyproject.read_text(encoding="utf-8")), encoding="utf-8")
    run(["uv", "build", "--wheel", "--out-dir", work / "dist", source], env, check=True)
    return work / "dist" / f"paravent-{version}-py3-none-any.whl"


def dir_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    work = Path(tempfile.mkdtemp(prefix="paravent-scenario-"))
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    env.update(UV_TOOL_DIR=str(work / "tools"), UV_TOOL_BIN_DIR=str(work / "bin"),
               PARAVENT_CACHE_DIR=str(work / "cache"), PYTHONUTF8="1")
    exe = work / "bin" / ("paravent.exe" if sys.platform == "win32" else "paravent")
    metrics: dict[str, str] = {}

    old_wheel, new_wheel = build_wheel(OLD, work, env), build_wheel(NEW, work, env)
    feed = work / "feed"
    feed.mkdir()
    (feed / "latest.json").write_text(json.dumps({
        "version": NEW,
        "wheel": new_wheel.as_uri(),
        "sha256": hashlib.sha256(new_wheel.read_bytes()).hexdigest(),
    }))
    env["PARAVENT_UPDATE_URL"] = (feed / "latest.json").as_uri()

    start = time.monotonic()
    run(["uv", "tool", "install", "--python", PYTHON, old_wheel], env, check=True)
    metrics["installation"] = f"{time.monotonic() - start:.0f} s"
    metrics["taille installée"] = f"{dir_size(work / 'tools') / 1e6:.0f} Mo"
    assert run([exe, "--version"], env, capture_output=True, check=True).stdout.strip() == OLD

    output = work / "courrier.md"
    start = time.monotonic()
    run([exe, "convert", ROOT / "tests" / "fixtures" / "courrier-scanne.pdf", "-o", output], env, check=True)
    metrics["conversion d'une page scannée (1er lancement)"] = f"{time.monotonic() - start:.1f} s"
    markdown = output.read_text(encoding="utf-8")
    print(markdown)
    assert "Élodie Lefèvre-Garçon" in markdown and "312,45 €" in markdown, "OCR output is wrong"

    start = time.monotonic()
    result = run([exe, "update", "--yes"], env, capture_output=True)
    print(result.stdout, result.stderr)
    assert result.returncode == 0, "paravent update failed"
    log_match = re.search(r"journal : (.+)\)", result.stdout)

    version = None
    while time.monotonic() - start < UPDATE_TIMEOUT:
        try:
            version = run([exe, "--version"], env, capture_output=True, timeout=60).stdout.strip()
        except (OSError, subprocess.TimeoutExpired) as error:
            print("not ready yet:", error)
        if version == NEW:
            break
        time.sleep(3)
    metrics["mise à jour"] = f"{time.monotonic() - start:.0f} s"
    if log_match:
        log = Path(log_match.group(1))
        for path in (log.with_name("update.ps1"), log):
            content = path.read_text(encoding="utf-8-sig", errors="replace") if path.exists() else "(absent)"
            print(f"--- {path.name} ---\n{content}")
    assert version == NEW, f"still on {version!r} after {UPDATE_TIMEOUT} s"

    report = "\n".join(f"| {name} | {value} |" for name, value in metrics.items())
    report = f"### Scénario d'installation ({sys.platform})\n\n| Étape | Mesure |\n|---|---|\n{report}\n"
    print(report)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(report)
    shutil.rmtree(work, ignore_errors=True)
    print(f"OK: {OLD} → {NEW}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
