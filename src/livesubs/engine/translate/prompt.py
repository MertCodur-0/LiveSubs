"""The chat messages for a subtitle translation, and cleanup of what the model returns.

Earlier lines go in as real user/assistant turns (English -> Turkish). Small models then keep the
dialogue's tone and terms consistent and never translate the context lines themselves, which they
tend to do when the context is pasted into one message.
"""

from __future__ import annotations

import re

from livesubs.engine.translate.base import TranslationRequest

SYSTEM_PROMPT = """\
You translate video game subtitles from English into Turkish.
- Write natural, spoken Turkish, the way Turkish players talk.
- Slang, swearing and street talk become natural Turkish of the same strength; never censor or \
soften them.
- Keep names of people, places, gangs, brands and apps exactly as written; do not translate or \
transliterate them.
- Keep it short enough to read as a subtitle.
- Reply with the Turkish translation of the user's line only: no quotes, notes, alternatives or \
English."""

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL)
_LABEL = re.compile(r"^(?:turkish|türkçe|translation|çeviri)\s*:\s*", re.IGNORECASE)
_QUOTES = "\"'“”‘’«»"


def build_messages(request: TranslationRequest) -> list[dict[str, str]]:
    system = SYSTEM_PROMPT
    if request.game_context.strip():
        system += f"\n\nAbout the game: {request.game_context.strip()}"
    if request.glossary:
        terms = "\n".join(f"- {en} -> {tr}" for en, tr in request.glossary.items())
        system += f"\n\nAlways translate these terms this way:\n{terms}"
    messages = [{"role": "system", "content": system}]
    for line in request.history:
        messages.append({"role": "user", "content": line.source})
        messages.append({"role": "assistant", "content": line.translation})
    messages.append({"role": "user", "content": request.text})
    return messages


def clean_output(text: str) -> str:
    """The translation alone: no reasoning block, label, wrapping quotes or line breaks."""
    text = _THINK_BLOCK.sub("", text).strip()
    text = _LABEL.sub("", text)
    text = " ".join(text.split())
    if len(text) >= 2 and text[0] in _QUOTES and text[-1] in _QUOTES:
        text = text[1:-1].strip()
    return text
