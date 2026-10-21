"""Cleaning OCR output, and a canonical key for comparing reads and caching translations."""

from __future__ import annotations

import re
import unicodedata

# Typographic punctuation folded to ASCII, by code point (ambiguous glyphs stay out of the source).
_FOLD = str.maketrans(
    {
        0x2018: "'",  # left single quote
        0x2019: "'",  # right single quote / apostrophe
        0x201A: "'",  # single low quote
        0x2032: "'",  # prime
        0x201C: '"',  # left double quote
        0x201D: '"',  # right double quote
        0x201E: '"',  # double low quote
        0x2026: "...",  # ellipsis
        0x2013: "-",  # en dash
        0x2014: " - ",  # em dash
        0x00A0: " ",  # no-break space
    }
)
# Windows OCR reads a capital I in sans-serif fonts as "|" or "l": "| don't", "l'm". Only a lone
# one followed by a word is an I; a lone trailing "|" is a box edge.
_PIPE_AS_I = re.compile(r"(?<![^\s\"(])[|l](?='\w|\s+[\w\"'(])")
# Stray marks OCR finds at the edges of a line (caption box corners, outline fragments).
_EDGE_JUNK = re.compile(r"^[\s|_~•·*=+^`<>\\/-]+|[\s|_~•·*=+^`<>\\/]+$")
_SPACES = re.compile(r"\s+")
_SPACE_BEFORE_PUNCT = re.compile(r"\s+([,.!?;:])")
_NOT_WORD = re.compile(r"[^\w\s]")


def clean(text: str) -> str:
    """The text to show and translate: folded punctuation, fixed I's, single spaces."""
    text = unicodedata.normalize("NFC", text).translate(_FOLD)
    text = _SPACES.sub(" ", text).strip()
    text = _PIPE_AS_I.sub("I", text)
    text = _EDGE_JUNK.sub("", text)
    return _SPACE_BEFORE_PUNCT.sub(r"\1", text)


def key(text: str) -> str:
    """Canonical form for comparisons and the translation cache: case, accents, punctuation and
    spacing do not matter ("Cabrón!" == "cabron")."""
    decomposed = unicodedata.normalize("NFKD", clean(text).casefold())
    letters = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return _SPACES.sub(" ", _NOT_WORD.sub("", letters)).strip()
