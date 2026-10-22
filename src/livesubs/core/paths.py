"""Where LiveSubs keeps its files.

Settings and profiles roam (``%APPDATA%\\LiveSubs``); the translation cache and logs are
machine-local (``%LOCALAPPDATA%\\LiveSubs``). ``LIVESUBS_HOME`` puts everything under one folder
instead, for tests and portable installs.
"""

import os
from pathlib import Path

import platformdirs

from livesubs import APP_NAME

HOME_ENV_VAR = "LIVESUBS_HOME"


def _override() -> Path | None:
    value = os.environ.get(HOME_ENV_VAR)
    return Path(value) if value else None


def config_dir() -> Path:
    if home := _override():
        return home
    return platformdirs.user_config_path(APP_NAME, appauthor=False, roaming=True)


def config_file() -> Path:
    return config_dir() / "config.json"


def profiles_dir() -> Path:
    return config_dir() / "profiles"


def cache_dir() -> Path:
    if home := _override():
        return home / "cache"
    return platformdirs.user_cache_path(APP_NAME, appauthor=False)


def translation_cache_file() -> Path:
    return cache_dir() / "translations.sqlite3"


def log_dir() -> Path:
    if home := _override():
        return home / "logs"
    return platformdirs.user_log_path(APP_NAME, appauthor=False)
