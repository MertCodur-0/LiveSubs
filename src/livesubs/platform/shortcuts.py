"""Desktop and Start menu shortcuts for the packaged app (dist\\LiveSubs\\LiveSubs.exe).

Uses the WScript.Shell COM object through PowerShell: it resolves the real Desktop folder (which
OneDrive may redirect) and needs no extra Python package.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from livesubs import APP_NAME

PACKAGED_EXE = "livesubs.exe"

_SCRIPT = """
$ErrorActionPreference = "Stop"
$shell = New-Object -ComObject WScript.Shell
$folders = [Environment]::GetFolderPath("Desktop"), [Environment]::GetFolderPath("Programs")
foreach ($folder in $folders) {
    $shortcut = $shell.CreateShortcut((Join-Path $folder '{name}.lnk'))
    $shortcut.TargetPath = '{target}'
    $shortcut.WorkingDirectory = '{folder}'
    $shortcut.Save()
}
"""


def packaged_exe() -> Path | None:
    """LiveSubs.exe when running from the packaged build, else None (development venv)."""
    exe = Path(sys.executable)
    return exe if exe.name.lower() == PACKAGED_EXE else None


def create_shortcuts(target: Path) -> bool:
    """Shortcuts to ``target`` on the desktop and in the Start menu; False when it failed."""
    script = _SCRIPT.replace("{name}", APP_NAME)
    script = script.replace("{target}", _quoted(target)).replace("{folder}", _quoted(target.parent))
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _quoted(path: Path) -> str:
    """Contents of a single-quoted PowerShell string ($ and backticks stay literal)."""
    return str(path).replace("'", "''")
