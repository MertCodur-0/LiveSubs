"""Installs Ollama for the user with winget (built into Windows 11), without admin rights.

Ollama's installer is per user and signed, so Smart App Control lets it run. Without winget the
UI opens the download page instead.
"""

from __future__ import annotations

import shutil
import subprocess

DOWNLOAD_PAGE = "https://ollama.com/download/windows"
WINGET_ID = "Ollama.Ollama"
INSTALL_TIMEOUT_S = 20 * 60  # the installer is ~1 GB (CUDA libraries)


def winget_path() -> str | None:
    return shutil.which("winget")


def install_command(winget: str) -> list[str]:
    return [
        winget,
        "install",
        "--id",
        WINGET_ID,
        "--exact",
        "--silent",
        "--accept-package-agreements",
        "--accept-source-agreements",
        "--disable-interactivity",
    ]


def install_ollama() -> tuple[bool, str]:
    """Run the installation (blocks for minutes). Returns success and winget's last output line."""
    winget = winget_path()
    if winget is None:
        return False, "winget not found"
    try:
        result = subprocess.run(
            install_command(winget),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=INSTALL_TIMEOUT_S,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return result.returncode == 0, lines[-1] if lines else f"exit code {result.returncode}"
