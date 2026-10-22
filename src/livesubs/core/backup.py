"""Settings backup: ``config.json`` and the profiles in one zip, to carry over a Windows reinstall.

The translation cache and logs are not included: they are machine-local and rebuilt on use.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

from livesubs.core import paths

CONFIG_NAME = "config.json"
_PROFILE = re.compile(r"profiles/[^/\\]+\.json")


class BackupError(Exception):
    pass


def _is_settings_file(name: str) -> bool:
    return name == CONFIG_NAME or _PROFILE.fullmatch(name) is not None


def export_settings(target: Path, source_dir: Path | None = None) -> int:
    """Write the settings to the zip ``target``; returns how many files went in."""
    source_dir = source_dir or paths.config_dir()
    files = [source_dir / CONFIG_NAME, *sorted((source_dir / "profiles").glob("*.json"))]
    files = [f for f in files if f.is_file()]
    if not files:
        raise BackupError("no settings to export yet")
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for file in files:
            archive.write(file, file.relative_to(source_dir).as_posix())
    return len(files)


def import_settings(archive_path: Path, target_dir: Path | None = None) -> int:
    """Restore settings from a zip made by ``export_settings``; returns how many files came in.

    Only ``config.json`` and ``profiles/*.json`` are taken; anything else in the zip is ignored.
    Existing files with the same names are replaced.
    """
    target_dir = target_dir or paths.config_dir()
    try:
        with zipfile.ZipFile(archive_path) as archive:
            names = [n for n in archive.namelist() if _is_settings_file(n)]
            if not names:
                raise BackupError("the file contains no LiveSubs settings")
            for name in names:
                destination = target_dir / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(archive.read(name))
    except (zipfile.BadZipFile, OSError) as exc:
        raise BackupError(str(exc)) from exc
    return len(names)
