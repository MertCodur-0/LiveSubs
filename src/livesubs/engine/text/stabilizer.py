"""When to read, and which subtitle lines are on screen.

Games stack lines: when two or three characters talk at once, every speaker's sentence gets a row
of its own, the newest at the bottom, and older rows scroll up and out. So the region is not one
subtitle but a short list of them. Every OCR read is split into its rows; rows that belong to one
sentence (``group_rows``) form one *line*; each line is tracked, confirmed and translated on its
own and keeps its ``line_id`` while it scrolls up. A line that was already translated is never
sent again because a new one appeared under it.

The pipeline feeds every change-detector verdict to ``update`` (which says whether to run OCR
now) and the rows of every OCR result to ``on_read``. Both return events:

- A new line is only confirmed after ``required_reads`` agreeing reads (fuzzy match on the
  normalized key), so a half-faded or glitched frame is never translated. While a new line waits
  for its confirming read (or a line seems to be gone), the next read is due ``fast_reread_s``
  later instead of at the next tick.
- While lines stay up they are re-read: at once when the text mask grows (glyphs that a bright
  background hid become visible), otherwise every ``upgrade_interval_s``. A read that adds words
  to a line, confirmed the same way, replaces it (``LineUpgraded``). Other variations are
  ignored, so the overlay never flickers between readings.
- A line missing from ``miss_reads`` reads in a row has left the screen. ``ScreenChanged`` lists
  the lines on screen, top to bottom, whenever that list changes (empty = no subtitle).
- A confirmed line that matches one that left the screen a moment ago (``RECENT_S``) gets that
  line's id back with ``repeat=True``: it reappeared after a flash, keep its translation.
- A line that continues the sentence of the line just before it (``text.sentences``; one
  subtitle replaced by the next piece) is merged into that line: ``LineUpgraded`` with the whole
  sentence under the earlier line's id.

All times are ``time.monotonic()`` seconds passed in by the caller.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from rapidfuzz import fuzz

from livesubs.engine.text import filters, sentences
from livesubs.engine.text.normalize import clean, key
from livesubs.engine.vision.change import Change

# How long a line that left the screen can come back as the same line (flash, flicker).
RECENT_S = 10.0
# Longest gap between two pieces of one sentence: from the last read that showed the previous
# piece to the next piece's confirmation.
MERGE_GAP_S = 1.5
# A read of a row this close to a tracked line is that line (OCR noise on a moving background).
TRACK_SIMILARITY = 80.0
# A row that is a part of a tracked line (its other rows scrolled out or were lost) needs at least
# this many words to count as that line ("No." must not stick to "No! Bad! No!"); a line needs as
# many to be upgraded by a longer read ("Yeah." -> "Yeah, I know." is the next line).
MIN_PART_WORDS = 3


@dataclass(frozen=True, slots=True)
class LineConfirmed:
    line_id: int
    text: str
    repeat: bool = False  # the line came back after a moment off screen: keep its translation


@dataclass(frozen=True, slots=True)
class LineUpgraded:
    line_id: int
    text: str  # the whole text to translate now (a better read, or the merged sentence)
    previous: str


@dataclass(frozen=True, slots=True)
class ScreenChanged:
    line_ids: tuple[int, ...]  # the lines on screen, top to bottom; empty: no subtitle


Event = LineConfirmed | LineUpgraded | ScreenChanged


@dataclass(frozen=True, slots=True)
class Decision:
    read: bool  # run OCR on this frame
    events: tuple[Event, ...] = ()


def is_upgrade(new: str, current: str, min_kept: float = 0.85) -> bool:
    """``new`` keeps (nearly) all of ``current``'s words in order and adds at least one."""
    new_words, current_words = key(new).split(), key(current).split()
    if len(new_words) <= len(current_words) or not current_words:
        return False
    matcher = SequenceMatcher(a=current_words, b=new_words, autojunk=False)
    kept = sum(block.size for block in matcher.get_matching_blocks())
    return kept >= min_kept * len(current_words)


def group_rows(rows: Iterable[str]) -> list[str]:
    """Join the rows of one sentence. A row continues the one above it when that row has no
    sentence end, or when it starts in lower case (a word of the end was lost); a new sentence
    in capitals is a new line - usually another speaker. Games that never punctuate therefore
    keep all rows together, as one subtitle."""
    lines: list[str] = []
    for row in rows:
        if lines and (sentences.is_open(lines[-1]) or sentences.starts_lower(row)):
            lines[-1] = f"{lines[-1]} {row}"
        else:
            lines.append(row)
    return lines


@dataclass
class _Candidate:
    text: str
    reads: int = 1


@dataclass
class _Line:
    line_id: int
    text: str  # what is on screen now (matched against new reads)
    pieces: list[str] = field(default_factory=list)  # earlier pieces of a merged sentence
    row: int = 0  # position among the lines of the last read that showed it
    misses: int = 0  # reads in a row without it
    seen: float = 0.0  # last read that showed it
    upgrade: _Candidate | None = None

    @property
    def full(self) -> str:
        return " ".join([*self.pieces, self.text])


@dataclass
class Stabilizer:
    required_reads: int = 2
    similarity: float = 90.0  # rapidfuzz ratio of the keys, 0-100
    fast_reread_s: float = 0.05
    upgrade_interval_s: float = 0.5
    mask_growth: float = 0.15  # relative growth of text pixels that triggers a re-read
    miss_reads: int = 2  # reads in a row without a line before it counts as gone
    merge_split_sentences: bool = True

    _lines: list[_Line] = field(default_factory=list, init=False)  # on screen
    _gone: list[_Line] = field(default_factory=list, init=False)  # left recently, newest last
    _candidates: list[_Candidate] = field(default_factory=list, init=False)
    _shown: tuple[int, ...] = field(default=(), init=False)  # last ScreenChanged
    _next_id: int = field(default=1, init=False)
    _next_read_at: float = field(default=0.0, init=False)
    _last_read_pixels: int = field(default=0, init=False)
    _pixels: int = field(default=0, init=False)

    @property
    def current(self) -> list[str]:
        """The texts of the lines on screen, top to bottom."""
        return [line.text for line in self._ordered()]

    @property
    def awaiting_confirmation(self) -> bool:
        """Something waits for its next read (a new line, an upgrade, a line that may be gone):
        the caller should look again soon (``fast_reread_s``) instead of at its normal tick."""
        return bool(self._candidates) or any(
            line.upgrade is not None or line.misses for line in self._lines
        )

    def update(self, change: Change, text_pixels: int, now: float) -> Decision:
        """Feed the change detector's verdict for a frame; says whether to read it."""
        self._pixels = text_pixels
        match change:
            case Change.NONE:
                return Decision(read=False)
            case Change.CLEARED:
                for line in self._lines:
                    self._leave(line)
                self._lines, self._candidates = [], []
                self._last_read_pixels = 0
                return Decision(read=False, events=self._screen_event())
            case Change.APPEARED | Change.CHANGED:
                # A row appeared, scrolled or left; the text decides which. Candidates stay: a
                # short line's small mask can come as CHANGED on every frame (a few pixels of
                # moving background), and dropping the candidate then never confirmed it.
                return Decision(read=True)
        # Change.SAME: read when due, or at once when more glyphs became visible.
        grew = text_pixels > self._last_read_pixels * (1 + self.mask_growth)
        return Decision(read=grew or now >= self._next_read_at)

    def on_read(self, rows: Iterable[str], now: float) -> list[Event]:
        """Feed the rows (top to bottom) of the OCR result of a frame ``update`` asked to read."""
        self._last_read_pixels = self._pixels
        texts = [clean(row) for row in rows]
        events = self._judge(group_rows(t for t in texts if filters.check(t).ok), now)
        delay = self.fast_reread_s if self.awaiting_confirmation else self.upgrade_interval_s
        self._next_read_at = now + delay
        return events

    # --- one read --------------------------------------------------------------------------------

    def _judge(self, texts: list[str], now: float) -> list[Event]:
        events: list[Event] = []
        matched: set[int] = set()
        unmatched: list[tuple[int, str]] = []
        for row, text in enumerate(texts):
            line = self._match(text, matched)
            if line is None:
                unmatched.append((row, text))
                continue
            matched.add(line.line_id)
            line.row, line.misses, line.seen = row, 0, now
            event = self._upgrade(line, text)
            if event is not None:
                events.append(event)

        for line in [line for line in self._lines if line.line_id not in matched]:
            line.upgrade = None
            line.misses += 1
            if line.misses >= self.miss_reads:
                self._lines.remove(line)
                self._leave(line)

        candidates: list[_Candidate] = []
        for row, text in unmatched:
            previous = next((c for c in self._candidates if self._similar(text, c.text)), None)
            candidate = _Candidate(text, previous.reads + 1 if previous else 1)
            if candidate.reads < self.required_reads:
                candidates.append(candidate)
            else:
                events.append(self._confirm(candidate.text, row, now, matched))
        self._candidates = candidates
        events.extend(self._screen_event())
        return events

    def _match(self, text: str, taken: set[int]) -> _Line | None:
        """The tracked line this read is (a better or a worse read of it), if any."""
        best, best_score = None, 0.0
        for line in self._lines:
            if line.line_id in taken:
                continue
            words, line_words = len(key(text).split()), len(key(line.text).split())
            score = fuzz.ratio(key(text), key(line.text))
            if line_words >= MIN_PART_WORDS and is_upgrade(text, line.text):
                score = 100.0
            elif MIN_PART_WORDS <= words <= line_words:
                # A part of the line: its other rows scrolled out, or words were lost.
                score = max(score, fuzz.partial_ratio(key(text), key(line.text)))
            if score >= TRACK_SIMILARITY and score > best_score:
                best, best_score = line, score
        return best

    def _upgrade(self, line: _Line, text: str) -> LineUpgraded | None:
        # A short line is rarely missing words, and "Yeah." -> "Yeah, I know." is a new line.
        short = len(key(line.text).split()) < MIN_PART_WORDS
        if short or not is_upgrade(text, line.text):
            line.upgrade = None  # just another reading of the line
            return None
        previous = line.upgrade
        if previous is not None and self._similar(text, previous.text):
            line.upgrade = _Candidate(text, previous.reads + 1)
        else:
            line.upgrade = _Candidate(text)
        if line.upgrade.reads < self.required_reads:
            return None
        before = line.full
        line.text, line.upgrade = text, None
        return LineUpgraded(line.line_id, line.full, previous=before)

    def _confirm(self, text: str, row: int, now: float, matched: set[int]) -> Event:
        back = next(
            (
                g
                for g in reversed(self._gone)
                if now - g.seen <= RECENT_S and self._similar(text, g.text)
            ),
            None,
        )
        if back is not None:  # it left a moment ago and came back
            self._gone.remove(back)
            self._show(back, row, now, matched)
            return LineConfirmed(back.line_id, back.full, repeat=True)
        merged = self._merge_target(text, now, matched)
        if merged is not None:
            before = merged.full
            merged.pieces.append(merged.text)
            merged.text = text
            self._show(merged, row, now, matched)
            return LineUpgraded(merged.line_id, merged.full, previous=before)
        line = _Line(self._next_id, text)
        self._next_id += 1
        self._show(line, row, now, matched)
        return LineConfirmed(line.line_id, text)

    def _merge_target(self, text: str, now: float, matched: set[int]) -> _Line | None:
        """The newest line, if ``text`` is the next piece of its sentence (it was replaced)."""
        if not self.merge_split_sentences or self._next_id == 1:
            return None
        newest = self._next_id - 1
        line = next((x for x in [*self._lines, *self._gone] if x.line_id == newest), None)
        if line is None or line.line_id in matched:
            return None  # gone for long, or still on screen above the new line
        ok = (
            len(line.pieces) + 1 < sentences.MAX_PIECES
            and now - line.seen <= MERGE_GAP_S
            and sentences.continues(line.text, text)
        )
        if not ok:
            return None
        if line in self._gone:
            self._gone.remove(line)
        else:
            self._lines.remove(line)
        return line

    def _show(self, line: _Line, row: int, now: float, matched: set[int]) -> None:
        line.row, line.misses, line.seen, line.upgrade = row, 0, now, None
        self._lines.append(line)
        matched.add(line.line_id)

    def _leave(self, line: _Line) -> None:
        line.upgrade = None
        self._gone = [g for g in self._gone if line.seen - g.seen <= RECENT_S]
        self._gone.append(line)

    # --- helpers ---------------------------------------------------------------------------------

    def _ordered(self) -> list[_Line]:
        return sorted(self._lines, key=lambda line: (line.row, line.line_id))

    def _screen_event(self) -> tuple[ScreenChanged, ...]:
        shown = tuple(line.line_id for line in self._ordered())
        if shown == self._shown:
            return ()
        self._shown = shown
        return (ScreenChanged(shown),)

    def _similar(self, a: str, b: str) -> bool:
        return fuzz.ratio(key(a), key(b)) >= self.similarity
