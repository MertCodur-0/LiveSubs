"""The chat messages for a subtitle translation, and cleanup of what the model returns.

A few example turns (slang English -> natural Turkish) come first: on the sample lines they
fixed literal slang ("dog" -> "köpek" became "kanka"). A profile may bring its own examples for
its game; otherwise the fixed ones below are used, which are deliberately not taken from the test
samples. Earlier lines of the dialogue follow as real user/assistant turns, so small models
keep the tone and terms consistent and never translate the context lines themselves, which they
tend to do when the context is pasted into one message.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence

from livesubs.engine.text.normalize import key
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

EXAMPLES: tuple[tuple[str, str], ...] = (
    ("Yo, dog, what's good?", "Naber kanka, ne var ne yok?"),
    ("Shut the fuck up and drive.", "Kapa lan çeneni de sür şu arabayı."),
    (
        "Get your ass in the car, bro. The cops are coming!",
        "Kıçını arabaya sok kanka. Aynasızlar geliyor!",
    ),
    ("Man, that's some bullshit right there.", "Yok artık, saçmalığın daniskası bu."),
    (
        "Tell Marcus I got the package from Little Havana.",
        "Marcus'a söyle, paketi Little Havana'dan aldım.",
    ),
)

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL)
_LABEL = re.compile(r"^(?:turkish|türkçe|translation|çeviri)\s*:\s*", re.IGNORECASE)
_QUOTES = "\"'“”‘’«»"


def relevant_terms(glossary: Mapping[str, str], text: str) -> dict[str, str]:
    """The glossary entries that occur in ``text`` as whole words, ignoring case, accents and
    punctuation; a plural or possessive "s"/"es" still matches ("Cops", "Lucia's").

    Sending only these keeps the prompt short (it is processed again for every line) and stops
    unrelated terms from leaking into a translation ("Raymond" became the glossary's "Ray")."""
    words = f" {key(text)} "
    terms = {}
    for en, tr in glossary.items():
        term = key(en)
        if term and re.search(rf" {re.escape(term)}(?:e?s)? ", words):
            terms[en] = tr
    return terms


def build_messages(request: TranslationRequest) -> list[dict[str, str]]:
    system = SYSTEM_PROMPT
    if request.game_context.strip():
        system += f"\n\nAbout the game: {request.game_context.strip()}"
    if glossary := relevant_terms(request.glossary, request.text):
        terms = "\n".join(f"- {en} -> {tr}" for en, tr in glossary.items())
        system += f"\n\nAlways translate these terms this way:\n{terms}"
    messages = [{"role": "system", "content": system}]
    for source, translation in request.examples or EXAMPLES:
        messages.append({"role": "user", "content": source})
        messages.append({"role": "assistant", "content": translation})
    for line in request.history:
        messages.append({"role": "user", "content": line.source})
        messages.append({"role": "assistant", "content": line.translation})
    messages.append({"role": "user", "content": request.text})
    return messages


def fingerprint(
    game_context: str, glossary: Mapping[str, str], examples: Sequence[tuple[str, str]]
) -> str:
    """A short digest of everything besides the line that shapes a translation: the system
    prompt, the profile's context, glossary and examples. Part of the cache key, so editing them
    (or importing a translation pack) never serves translations made without them."""
    payload = json.dumps(
        [SYSTEM_PROMPT, game_context.strip(), sorted(glossary.items()), list(examples or EXAMPLES)],
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


def clean_output(text: str) -> str:
    """The translation alone: no reasoning block, label, wrapping quotes or line breaks."""
    text = _THINK_BLOCK.sub("", text).strip()
    text = _LABEL.sub("", text)
    text = " ".join(text.split())
    if len(text) >= 2 and text[0] in _QUOTES and text[-1] in _QUOTES:
        text = text[1:-1].strip()
    return text
