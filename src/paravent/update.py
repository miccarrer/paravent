"""Self-update from the GitHub releases feed.

Paravent is installed as a uv tool. A release publishes a wheel and a
``latest.json`` manifest (version, wheel URL, sha256). Updating means
downloading the wheel, checking its hash, then asking uv to reinstall the
tool from it.

On Windows the running ``paravent.exe`` is locked and cannot be replaced
while it runs, so the reinstall is handed to a detached PowerShell process
that waits for this one to exit first. A Paravent started meanwhile would
lock the files again and leave the tool half removed: it finds a marker file
and stops at once, and the script retries while any process still runs from
the tool's folder.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse

DEFAULT_MANIFEST_URL = "https://github.com/miccarrer/paravent/releases/latest/download/latest.json"
TIMEOUT = 30
INSTALL_ATTEMPTS = 5
MARKER = "mise-a-jour-en-cours"
# A marker older than this was left by a script that died: ignore it.
MARKER_MAX_AGE = 15 * 60


class UpdateError(RuntimeError):
    pass


@dataclass
class Release:
    version: str
    wheel_url: str
    sha256: str

    @property
    def wheel_name(self) -> str:
        return Path(urlparse(self.wheel_url).path).name


def manifest_url() -> str:
    return os.environ.get("PARAVENT_UPDATE_URL", DEFAULT_MANIFEST_URL)


def parse_version(version: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in version.strip().lstrip("v").split("."))
    except ValueError:
        raise UpdateError(f"Numéro de version invalide : {version!r}") from None


def is_newer(candidate: str, current: str) -> bool:
    return parse_version(candidate) > parse_version(current)


def fetch_latest(url: str | None = None) -> Release:
    url = url or manifest_url()
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as response:
            data = json.load(response)
        # A relative wheel URL is resolved against the manifest's own URL.
        return Release(data["version"], urljoin(url, data["wheel"]), data["sha256"].lower())
    except UpdateError:
        raise
    except (OSError, ValueError, KeyError) as error:
        raise UpdateError(f"Impossible de lire les informations de mise à jour ({error}).") from error


def download(release: Release, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / release.wheel_name
    digest = hashlib.sha256()
    try:
        with urllib.request.urlopen(release.wheel_url, timeout=TIMEOUT) as response, open(target, "wb") as out:
            while chunk := response.read(1 << 16):
                digest.update(chunk)
                out.write(chunk)
    except OSError as error:
        target.unlink(missing_ok=True)
        raise UpdateError(f"Téléchargement impossible ({error}).") from error
    if digest.hexdigest() != release.sha256:
        target.unlink(missing_ok=True)
        raise UpdateError("Le fichier téléchargé est corrompu ou a été modifié : mise à jour annulée.")
    return target


def cache_dir() -> Path:
    if override := os.environ.get("PARAVENT_CACHE_DIR"):
        return Path(override)
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / "paravent" / "updates"


def in_progress() -> bool:
    """True while a deferred Windows update is being installed."""
    try:
        age = time.time() - (cache_dir() / MARKER).stat().st_mtime
    except OSError:
        return False
    return age < MARKER_MAX_AGE


def find_uv() -> str:
    uv = os.environ.get("PARAVENT_UV") or shutil.which("uv")
    if not uv:
        raise UpdateError("uv est introuvable : Paravent doit être installé avec uv pour se mettre à jour.")
    return uv


def installed_as_uv_tool(uv: str) -> bool:
    try:
        tool_dir = subprocess.run([uv, "tool", "dir"], capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return False
    return Path(sys.prefix).resolve().is_relative_to(Path(tool_dir).resolve())


def install_command(uv: str, wheel: Path) -> list[str]:
    # Keep the interpreter the tool already runs on: dependencies such as
    # onnxruntime do not follow every new Python release straight away.
    python = f"{sys.version_info.major}.{sys.version_info.minor}"
    return [uv, "tool", "install", "--force", "--python", python, str(wheel)]


def install(wheel: Path, uv: str) -> Path | None:
    """Reinstall the tool from ``wheel``.

    Returns None once installed (POSIX), or the log file path when the
    install was deferred to a detached process (Windows).
    """
    command = install_command(uv, wheel)
    if sys.platform != "win32":
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0:
            raise UpdateError(f"L'installation a échoué :\n{result.stderr.strip()}")
        return None
    return _install_deferred_windows(command, wheel.parent)


def _install_deferred_windows(command: list[str], work_dir: Path) -> Path:
    log = work_dir / "update.log"
    script = work_dir / "update.ps1"
    # Wait for this interpreter and for the uv launcher (paravent.exe) that
    # started it: both hold files the reinstall must replace.
    pids = sorted({os.getpid(), os.getppid()})
    marker = cache_dir() / MARKER
    # Windows PowerShell 5.1 reads a BOM-less script as ANSI, which would
    # mangle a non-ASCII profile path (C:\Users\Hélène\...): write a BOM.
    script.write_text(windows_update_script(command, log, pids, Path(sys.prefix), marker), encoding="utf-8-sig")
    log.unlink(missing_ok=True)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(str(os.getpid()))
    # No DETACHED_PROCESS: PowerShell without any console may die on start.
    # A hidden console of its own (CREATE_NO_WINDOW) in a separate process
    # group keeps it alive once Paravent exits.
    subprocess.Popen(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)],
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    return log


def windows_update_script(command: list[str], log: Path, pids: list[int], tool_dir: Path, marker: Path) -> str:
    def quote(value: object) -> str:
        return "'" + str(value).replace("'", "''") + "'"

    tool_prefix = str(tool_dir).rstrip("\\") + "\\"  # so that C:\...\paravent does not match paravent-old
    return (
        f"function Log($line) {{ Add-Content -Path {quote(log)} -Value $line -Encoding UTF8 }}\n"
        f"$toolDir = {quote(tool_prefix)}\n"
        'Log "started $(Get-Date -Format o)"\n'
        f"Wait-Process -Id {','.join(map(str, pids))} -Timeout 120 -ErrorAction SilentlyContinue\n"
        f"for ($attempt = 1; $attempt -le {INSTALL_ATTEMPTS}; $attempt++) {{\n"
        # Anything still running from the tool's folder holds its files.
        "  $busy = Get-Process -ErrorAction SilentlyContinue | Where-Object {\n"
        "    $_.Path -and $_.Path.StartsWith($toolDir, [StringComparison]::OrdinalIgnoreCase) }\n"
        "  if ($busy) {\n"
        '    Log "waiting for $(@($busy).Count) process(es) from the tool folder"\n'
        "    $busy | Wait-Process -Timeout 60 -ErrorAction SilentlyContinue\n"
        "  }\n"
        '  Log "installing, attempt $attempt, $(Get-Date -Format o)"\n'
        # PowerShell 5.1 wraps each stderr line of a native command in an
        # ErrorRecord; turn them back into plain text for the log.
        f"  $output = & {' '.join(map(quote, command))} 2>&1 | ForEach-Object {{ \"$_\" }} | Out-String\n"
        "  $code = $LASTEXITCODE\n"
        "  Log $output\n"
        "  if ($code -eq 0) { break }\n"
        "  Start-Sleep -Seconds 5\n"
        "}\n"
        f"Remove-Item -Path {quote(marker)} -Force -ErrorAction SilentlyContinue\n"
        'Log "paravent-update-exit=$code"\n'
    )


def new_download_dir() -> Path:
    base = cache_dir()
    base.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="release-", dir=base))
