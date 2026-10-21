"""User-facing text, kept in one place so the UI can be translated later.

``t("key", **values)`` returns the Turkish text with ``str.format`` placeholders filled in.
"""

from __future__ import annotations

from livesubs.i18n.tr import STRINGS


def t(key: str, **values: object) -> str:
    text = STRINGS.get(key, key)
    return text.format(**values) if values else text
