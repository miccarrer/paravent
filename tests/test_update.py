import hashlib
import json
import sys
from pathlib import Path, PureWindowsPath

import pytest

from paravent import update


@pytest.mark.parametrize(
    ("candidate", "current", "expected"),
    [("0.2.0", "0.1.0", True), ("0.10.0", "0.9.9", True), ("v1.0.0", "0.9.0", True),
     ("0.1.0", "0.1.0", False), ("0.1.0", "0.2.0", False)],
)
def test_is_newer(candidate, current, expected):
    assert update.is_newer(candidate, current) is expected


def test_invalid_version():
    with pytest.raises(update.UpdateError):
        update.parse_version("1.0-beta")


def publish(tmp_path, payload: bytes, sha256: str | None = None):
    """Lay out a fake release feed and return the manifest URL."""
    (tmp_path / "paravent-9.9.9-py3-none-any.whl").write_bytes(payload)
    manifest = tmp_path / "latest.json"
    manifest.write_text(json.dumps({
        "version": "9.9.9",
        "wheel": "paravent-9.9.9-py3-none-any.whl",
        "sha256": sha256 or hashlib.sha256(payload).hexdigest(),
    }))
    return manifest.as_uri()


def test_fetch_latest_resolves_relative_wheel(tmp_path):
    release = update.fetch_latest(publish(tmp_path, b"wheel"))
    assert release.version == "9.9.9"
    assert release.wheel_url == (tmp_path / "paravent-9.9.9-py3-none-any.whl").as_uri()
    assert release.wheel_name == "paravent-9.9.9-py3-none-any.whl"


def test_fetch_latest_rejects_incomplete_manifest(tmp_path):
    manifest = tmp_path / "latest.json"
    manifest.write_text('{"version": "1.0.0"}')
    with pytest.raises(update.UpdateError):
        update.fetch_latest(manifest.as_uri())


def test_download_checks_hash(tmp_path):
    (tmp_path / "feed").mkdir()
    release = update.fetch_latest(publish(tmp_path / "feed", b"wheel"))
    wheel = update.download(release, tmp_path / "dl")
    assert wheel.read_bytes() == b"wheel"


def test_download_rejects_tampered_file(tmp_path):
    (tmp_path / "feed").mkdir()
    release = update.fetch_latest(publish(tmp_path / "feed", b"wheel", sha256="0" * 64))
    with pytest.raises(update.UpdateError, match="corrompu"):
        update.download(release, tmp_path / "dl")
    assert not (tmp_path / "dl" / release.wheel_name).exists()


def test_install_command_keeps_current_python(tmp_path):
    command = update.install_command("uv", tmp_path / "p.whl")
    assert command[:4] == ["uv", "tool", "install", "--force"]
    assert command[command.index("--python") + 1] == f"{sys.version_info.major}.{sys.version_info.minor}"


def test_windows_script_quotes_paths_with_accents_and_apostrophes():
    log = PureWindowsPath(r"C:\Users\Hélène d'Arc\AppData\Local\paravent\updates\update.log")
    tool_dir = PureWindowsPath(r"C:\Users\Hélène d'Arc\AppData\Roaming\uv\tools\paravent")
    script = update.windows_update_script(["uv", "tool", "install", r"C:\Users\Hélène d'Arc\p.whl"], log, [12, 34],
                                          tool_dir, log.with_name("mise-a-jour-en-cours"))
    assert "Wait-Process -Id 12,34 -Timeout 120" in script
    assert r"'C:\Users\Hélène d''Arc\p.whl'" in script
    assert r"Add-Content -Path 'C:\Users\Hélène d''Arc\AppData" in script
    assert r"$toolDir = 'C:\Users\Hélène d''Arc\AppData\Roaming\uv\tools\paravent\'" in script
    assert f"$attempt -le {update.INSTALL_ATTEMPTS}" in script
    assert r"Remove-Item -Path 'C:\Users\Hélène d''Arc\AppData\Local\paravent\updates\mise-a-jour-en-cours'" in script
    assert script.rstrip().endswith('Log "paravent-update-exit=$code"')


def test_update_in_progress_marker(tmp_path, monkeypatch):
    import os
    import time

    monkeypatch.setenv("PARAVENT_CACHE_DIR", str(tmp_path))
    assert not update.in_progress()
    marker = tmp_path / update.MARKER
    marker.write_text("1")
    assert update.in_progress()
    old = time.time() - update.MARKER_MAX_AGE - 1
    os.utime(marker, (old, old))
    assert not update.in_progress()  # left behind by a script that died
