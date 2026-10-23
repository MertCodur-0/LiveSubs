"""Translation packs: a JSON file that fills a profile's translation settings in one go.

A pack is prepared outside the app (by hand or with an assistant) for one game::

    {
      "game_context": "Where the game is set, who talks, how to translate.",
      "glossary": [{"en": "Vice City", "tr": "Vice City"}, ...],
      "examples": [{"en": "Yo, what's good?", "tr": "Naber?"}, ...],
      "test_lines": [{"en": "A line to compare models on."}, ...]
    }

``glossary`` and ``examples`` may also be plain ``{"English": "Turkish"}`` objects. The profile
takes context, glossary and examples; ``test_lines`` only feed model comparisons.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from livesubs.core.profiles import TranslationSettings


class PackError(ValueError):
    """The file is not a usable translation pack (the message says why)."""


@dataclass(frozen=True, slots=True)
class TranslationPack:
    translation: TranslationSettings
    test_lines: tuple[str, ...] = ()


def _pairs(value: object, field: str) -> dict[str, str]:
    if value is None:
        return {}
    if isinstance(value, dict):
        items = list(value.items())
    elif isinstance(value, list):
        items = []
        for entry in value:
            if not isinstance(entry, dict):
                raise PackError(f"{field}: every entry needs 'en' and 'tr'")
            items.append((entry.get("en"), entry.get("tr")))
    else:
        raise PackError(f"{field}: expected a list or an object")
    pairs = {}
    for english, turkish in items:
        if not isinstance(english, str) or not isinstance(turkish, str):
            raise PackError(f"{field}: every entry needs 'en' and 'tr' as text")
        if english.strip() and turkish.strip():
            pairs[english.strip()] = turkish.strip()
    return pairs


def _lines(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise PackError("test_lines: expected a list")
    lines = []
    for entry in value:
        text = entry.get("en") if isinstance(entry, dict) else entry
        if not isinstance(text, str):
            raise PackError("test_lines: every entry needs 'en' as text")
        if text.strip():
            lines.append(text.strip())
    return tuple(dict.fromkeys(lines))  # duplicates dropped, order kept


def parse_pack(text: str) -> TranslationPack:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PackError(f"not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise PackError("expected a JSON object")
    context = data.get("game_context", "")
    if not isinstance(context, str):
        raise PackError("game_context: expected text")
    translation = TranslationSettings(
        context=context.strip(),
        glossary=_pairs(data.get("glossary"), "glossary"),
        examples=_pairs(data.get("examples"), "examples"),
    )
    return TranslationPack(translation, _lines(data.get("test_lines")))


def load_pack(path: Path) -> TranslationPack:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise PackError(str(exc)) from exc
    return parse_pack(text)
