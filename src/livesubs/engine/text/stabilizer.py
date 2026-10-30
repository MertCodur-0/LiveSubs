"""When to read, and when a read becomes the subtitle to translate.

The pipeline feeds every change-detector verdict to ``update`` (which says whether to run OCR
now) and every OCR result to ``on_read`` (which may emit an event):

- A new line is only confirmed after ``required_reads`` agreeing reads (fuzzy match on the
  normalized key), so a half-faded or glitched frame is never translated. After a change a fast
  re-read is due ``fast_reread_s`` later instead of at the next tick.
- While the same line stays up it is re-read: at once when its text mask grows (glyphs that a
  bright background hid become visible), otherwise every ``upgrade_interval_s``. A read that adds
  words to the confirmed text, confirmed the same way, replaces it (``LineUpgraded``). Other
  variations are ignored, so the overlay never flickers between readings.
- ``LineConfirmed.repeat`` marks a line that matches the previous one (it reappeared after a
  brief gap), so the consumer can reuse the shown translation.

All times are ``time.monotonic()`` seconds passed in by the caller.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher

from rapidfuzz import fuzz

from livesubs.engine.text import filters
from livesubs.engine.text.normalize import clean, key
from livesubs.engine.vision.change import Change


@dataclass(frozen=True, slots=True)
class LineConfirmed:
    text: str
    repeat: bool = False  # same as the previous line: keep its translation


@dataclass(frozen=True, slots=True)
class LineUpgraded:
    text: str
    previous: str


@dataclass(frozen=True, slots=True)
class LineCleared:
    pass


Event = LineConfirmed | LineUpgraded | LineCleared


@dataclass(frozen=True, slots=True)
class Decision:
    read: bool  # run OCR on this frame
    event: Event | None = None


def is_upgrade(new: str, current: str, min_kept: float = 0.85) -> bool:
    """``new`` keeps (nearly) all of ``current``'s words in order and adds at least one."""
    new_words, current_words = key(new).split(), key(current).split()
    if len(new_words) <= len(current_words) or not current_words:
        return False
    matcher = SequenceMatcher(a=current_words, b=new_words, autojunk=False)
    kept = sum(block.size for block in matcher.get_matching_blocks())
    return kept >= min_kept * len(current_words)


@dataclass
class _Candidate:
    text: str
    reads: int = 1


@dataclass
class Stabilizer:
    required_reads: int = 2
    similarity: float = 90.0  # rapidfuzz ratio of the keys, 0-100
    fast_reread_s: float = 0.05
    upgrade_interval_s: float = 0.5
    mask_growth: float = 0.15  # relative growth of text pixels that triggers a re-read

    _current: str | None = field(default=None, init=False)  # confirmed line on screen
    _previous: str | None = field(default=None, init=False)  # last confirmed line, kept on clear
    _pending: bool = field(default=False, init=False)  # a new line awaits confirmation
    _candidate: _Candidate | None = field(default=None, init=False)
    _next_read_at: float = field(default=0.0, init=False)
    _last_read_pixels: int = field(default=0, init=False)
    _pixels: int = field(default=0, init=False)

    @property
    def current(self) -> str | None:
        return self._current

    @property
    def awaiting_confirmation(self) -> bool:
        """A candidate waits for its confirming read: the caller should look again soon
        (``fast_reread_s``) instead of at its normal tick."""
        return self._candidate is not None

    def update(self, change: Change, text_pixels: int, now: float) -> Decision:
        """Feed the change detector's verdict for a frame; says whether to read it."""
        self._pixels = text_pixels
        match change:
            case Change.NONE:
                return Decision(read=False)
            case Change.CLEARED:
                had_line = self._current is not None or self._pending
                self._reset()
                return Decision(read=False, event=LineCleared() if had_line else None)
            case Change.APPEARED | Change.CHANGED:
                # The candidate stays: the text decides whether this is still the same line. A
                # short line's small mask can come as CHANGED on every frame (a few pixels of
                # moving background), and dropping the candidate then never confirmed it.
                self._pending = True
                return Decision(read=True)
        # Change.SAME: read when due, or at once when more glyphs became visible.
        grew = text_pixels > self._last_read_pixels * (1 + self.mask_growth)
        return Decision(read=grew or now >= self._next_read_at)

    def on_read(self, raw_text: str, now: float) -> Event | None:
        """Feed the OCR result of a frame ``update`` asked to read."""
        self._last_read_pixels = self._pixels
        event = self._judge(clean(raw_text))
        # Hurry only while a candidate waits for its confirming read; otherwise poll slowly.
        delay = self.fast_reread_s if self._candidate is not None else self.upgrade_interval_s
        self._next_read_at = now + delay
        return event

    def _judge(self, text: str) -> Event | None:
        if not filters.check(text).ok:
            self._candidate = None
            return None
        if not self._pending and self._current is not None and not is_upgrade(text, self._current):
            self._candidate = None  # just another reading of the confirmed line
            return None
        if self._candidate is not None and self._similar(text, self._candidate.text):
            self._candidate = _Candidate(text, self._candidate.reads + 1)
        else:
            self._candidate = _Candidate(text)
        if self._candidate.reads < self.required_reads:
            return None

        confirmed, self._candidate = self._candidate.text, None
        if self._pending:
            self._pending = False
            repeat = self._previous is not None and self._similar(confirmed, self._previous)
            self._current = self._previous = confirmed
            return LineConfirmed(confirmed, repeat=repeat)
        previous, self._current = self._current, confirmed
        self._previous = confirmed
        return LineUpgraded(confirmed, previous=previous or "")

    def _similar(self, a: str, b: str) -> bool:
        return fuzz.ratio(key(a), key(b)) >= self.similarity

    def _reset(self) -> None:
        self._current = None
        self._pending = False
        self._candidate = None
        self._last_read_pixels = 0
