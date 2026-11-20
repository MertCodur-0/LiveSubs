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
  to a line, confirmed the same way, replaces it (``LineUpgraded``).
- With a ``scorer`` (``text.quality``), the best-scoring of a new line's agreeing reads is
  confirmed, not the latest, and a later read of a line that scores ``correct_margin`` higher
  corrects it (also ``LineUpgraded``): OCR garbles a line differently from frame to frame
  ("Heyl Get outl" / "Hey! Get out!"), so each line converges on its cleanest read. Without a
  scorer, or within the margin, other variations are ignored, so the overlay never flickers.
- A line missing from ``miss_reads`` reads in a row has left the screen. ``ScreenChanged`` lists
  the lines on screen, top to bottom, whenever that list changes (empty = no subtitle).
- A confirmed line that matches one that left the screen a moment ago (``RECENT_S``) gets that
  line's id back with ``repeat=True``: it reappeared after a flash, keep its translation.
- A line that continues the sentence of the line just before it (``text.sentences``; one
  subtitle replaced by the next piece) is merged into that line: ``LineUpgraded`` with the whole
  sentence under the earlier line's id.
- ``expected`` is the text that goes out for translation if the next read agrees with the new
  line read once: the caller may start translating it early, and must drop that work when
  ``expected`` changes without the line being confirmed.

All times are ``time.monotonic()`` seconds passed in by the caller.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
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
# Rows of one wrapped sentence are centred under each other; a row whose centre is further off
# (share of the region's width) is something else.
MAX_CENTER_SHIFT = 0.05
# ... and stacked closely: an empty gap of more than this many row heights separates two things.
MAX_ROW_GAP = 1.0
# Each read of a line on screen is a vote for its text (by key); a text read more often than
# another gains this much score per extra read, up to MAX_VOTES of them. Windows OCR misreads a
# glyph on some frames only ("Coma on" once, "Come on" four times), and both may be words.
VOTE_VALUE = 0.25
MAX_VOTES = 4


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


# A box on the region: left, top, right, bottom, each 0-1 of the region's width / height.
Box = tuple[float, float, float, float]


def union(a: Box | None, b: Box | None) -> Box | None:
    if a is None or b is None:
        return a or b
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


@dataclass(frozen=True, slots=True)
class Row:
    """One OCR row: its text, its horizontal centre (0-1 across the region) and its box's top and
    height (pixels of the read; only compared within one read). None = unknown. ``box`` is where
    it is on the region (``Box``), for placing the translation on the subtitle."""

    text: str
    center: float | None = None
    top: float | None = None
    height: float | None = None
    box: Box | None = None

    def far_below(self, above: Row) -> bool:
        """More than ``MAX_ROW_GAP`` row heights of empty space between ``above`` and this row:
        not the next row of the same subtitle (a weapon prompt high above an objective)."""
        if None in (self.top, self.height, above.top, above.height):
            return False
        gap = self.top - (above.top + above.height)
        return gap > MAX_ROW_GAP * max(self.height, above.height)


def group_rows(rows: Iterable[str | Row]) -> list[str]:
    """Join the rows of one sentence. A row continues the one above it when that row has no
    sentence end, or when it starts in lower case (a word of the end was lost); a new sentence
    in capitals is a new line - usually another speaker. Games that never punctuate therefore
    keep all rows together, as one subtitle. Subtitle rows are centred under each other: a row
    whose centre is off by more than ``MAX_CENTER_SHIFT`` (a HUD label, a watermark at the side),
    or far below the row above (``Row.far_below``), never joins a sentence."""
    return [text for text, _ in _grouped(rows)]


def _grouped(rows: Iterable[str | Row]) -> list[tuple[str, Box | None]]:
    """``group_rows`` with each line's box (the union of its rows' boxes)."""
    lines: list[tuple[str, Box | None]] = []
    previous: Row | None = None
    for item in rows:
        row = item if isinstance(item, Row) else Row(item)
        aligned = (
            previous is None
            or row.center is None
            or previous.center is None
            or abs(row.center - previous.center) <= MAX_CENTER_SHIFT
        ) and not (previous is not None and row.far_below(previous))
        if (
            lines
            and aligned
            and (sentences.is_open(lines[-1][0]) or sentences.starts_lower(row.text))
        ):
            text, box = lines[-1]
            lines[-1] = (f"{text} {row.text}", union(box, row.box))
        else:
            lines.append((row.text, row.box))
        previous = row
    return lines


@dataclass
class _Candidate:
    text: str
    score: float = 0.0
    reads: int = 1

    def agree(self, text: str, score: float) -> _Candidate:
        """One more agreeing read: keep the best one (the latest when nothing scores reads)."""
        best = (text, score) if score >= self.score else (self.text, self.score)
        return _Candidate(*best, reads=self.reads + 1)


@dataclass
class _Vote:
    text: str  # the best-scoring read of this key
    score: float
    reads: int = 0

    def add(self, text: str, score: float) -> None:
        self.reads += 1
        if score > self.score:
            self.text, self.score = text, score

    @property
    def value(self) -> float:
        return self.score + VOTE_VALUE * min(self.reads - 1, MAX_VOTES)


@dataclass
class _Line:
    line_id: int
    text: str  # what is on screen now (matched against new reads)
    score: float = 0.0  # the scorer's verdict on ``text``
    pieces: list[str] = field(default_factory=list)  # earlier pieces of a merged sentence
    row: int = 0  # position among the lines of the last read that showed it
    misses: int = 0  # reads in a row without it
    seen: float = 0.0  # last read that showed it
    upgrade: _Candidate | None = None
    votes: dict[str, _Vote] = field(default_factory=dict)  # reads of ``text``, by key
    box: Box | None = None  # where the last read that showed it found it

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
    scorer: Callable[[str], float] | None = None  # read quality, higher = better
    correct_margin: float = 0.5  # how much better a read must score to correct a line

    _lines: list[_Line] = field(default_factory=list, init=False)  # on screen
    _gone: list[_Line] = field(default_factory=list, init=False)  # left recently, newest last
    _candidates: list[_Candidate] = field(default_factory=list, init=False)
    _shown: tuple[int, ...] = field(default=(), init=False)  # last ScreenChanged
    _expected: str | None = field(default=None, init=False)
    _next_id: int = field(default=1, init=False)
    _next_read_at: float = field(default=0.0, init=False)
    _last_read_pixels: int = field(default=0, init=False)
    _pixels: int = field(default=0, init=False)
    _boxes: list[Box | None] = field(default_factory=list, init=False)  # of the current read

    @property
    def current(self) -> list[str]:
        """The texts of the lines on screen, top to bottom."""
        return [line.text for line in self._ordered()]

    @property
    def box(self) -> Box | None:
        """Where the subtitle is on the region: the newest line on screen and the lines stacked
        with it (centred within ``MAX_CENTER_SHIFT`` of it, no more than a line's height of
        space between them). A HUD label or a watermark elsewhere in the region is no part of
        it. None when no line is on screen or the reads carried no boxes."""
        lines = [line for line in self._lines if line.box is not None]
        if not lines:
            return None
        newest = max(lines, key=lambda line: line.line_id)
        box = newest.box
        assert box is not None
        center = (box[0] + box[2]) / 2
        aligned = sorted(
            (
                line.box
                for line in lines
                if line is not newest
                and line.box is not None
                and abs((line.box[0] + line.box[2]) / 2 - center) <= MAX_CENTER_SHIFT
            ),
            key=lambda b: abs((b[1] + b[3]) / 2 - (box[1] + box[3]) / 2),
        )
        for other in aligned:  # nearest first, so a stack is joined row by row
            assert box is not None
            gap = max(other[1] - box[3], box[1] - other[3])
            if gap <= min(other[3] - other[1], box[3] - box[1]):
                box = union(box, other)
        return box

    @property
    def expected(self) -> str | None:
        """The text a confirming read of the topmost new line would send for translation (the
        whole sentence when the line continues one); None when no new line waits, or when it is
        a line coming back (its translation is kept)."""
        return self._expected

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
                self._lines, self._candidates, self._expected = [], [], None
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

    def on_read(self, rows: Iterable[str | Row], now: float) -> list[Event]:
        """Feed the rows (top to bottom) of the OCR result of a frame ``update`` asked to read."""
        self._last_read_pixels = self._pixels
        cleaned = (
            replace(r, text=clean(r.text)) if isinstance(r, Row) else Row(clean(r)) for r in rows
        )
        events = self._judge(_grouped(r for r in cleaned if filters.check(r.text).ok), now)
        delay = self.fast_reread_s if self.awaiting_confirmation else self.upgrade_interval_s
        self._next_read_at = now + delay
        return events

    # --- one read --------------------------------------------------------------------------------

    def _judge(self, read: list[tuple[str, Box | None]], now: float) -> list[Event]:
        events: list[Event] = []
        matched: set[int] = set()
        unmatched: list[tuple[int, str]] = []
        boxes = [box for _, box in read]
        self._boxes = boxes
        for row, (text, box) in enumerate(read):
            line = self._match(text, matched)
            if line is None:
                unmatched.append((row, text))
                continue
            matched.add(line.line_id)
            line.row, line.misses, line.seen, line.box = row, 0, now, box
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
            score = self._score(text)
            previous = next((c for c in self._candidates if self._similar(text, c.text)), None)
            candidate = previous.agree(text, score) if previous else _Candidate(text, score)
            if candidate.reads < self.required_reads:
                candidates.append(candidate)
            else:
                events.append(self._confirm(candidate, row, now, matched))
        self._candidates = candidates
        self._expected = self._outcome(candidates[0].text, now, matched) if candidates else None
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
            if is_upgrade(text, line.text):
                # More words around all of the line's: a better read of it - or, for a short
                # line, a new line glued to it ("Okay." under an unpunctuated label).
                score = 100.0 if line_words >= MIN_PART_WORDS else 0.0
            elif MIN_PART_WORDS <= words <= line_words:
                # A part of the line: its other rows scrolled out, or words were lost.
                score = max(score, fuzz.partial_ratio(key(text), key(line.text)))
            if score >= TRACK_SIMILARITY and score > best_score:
                best, best_score = line, score
        return best

    def _upgrade(self, line: _Line, text: str) -> LineUpgraded | None:
        score = self._score(text)
        # A short line is rarely missing words, and "Yeah." -> "Yeah, I know." is a new line.
        short = len(key(line.text).split()) < MIN_PART_WORDS
        if short or not is_upgrade(text, line.text):
            line.upgrade = None  # another reading of the line: maybe a cleaner one
            # Not fewer words: a garbled word can push a read of the same line under
            # ``similarity`` ("fuckiJ1' left" / "fuckin' left me...").
            fuller = len(key(text).split()) >= len(key(line.text).split())
            if self._similar(text, line.text) or fuller:
                return self._correct(line, text, score)
            return None
        previous = line.upgrade
        if previous is not None and self._similar(text, previous.text):
            line.upgrade = previous.agree(text, score)
        else:
            line.upgrade = _Candidate(text, score)
        if line.upgrade.reads < self.required_reads:
            return None
        upgrade, line.upgrade = line.upgrade, None
        return self._replace(line, upgrade.text, upgrade.score)

    def _correct(self, line: _Line, text: str, score: float) -> LineUpgraded | None:
        """A read of a line on screen replaces it when it scores clearly better - counting the
        reads that agree with it (``VOTE_VALUE``)."""
        if self.scorer is None:
            return None
        current = line.votes.setdefault(key(line.text), _Vote(line.text, line.score, 1))
        line.votes.setdefault(key(text), _Vote(text, score)).add(text, score)
        # Never back to a text the line was upgraded from: its earlier votes would win it back
        # and the next reads upgrade it again - a new translation every read (seen on a weapon
        # prompt above a GTA objective, "GIRARDI ES9 PISTOL" scoring low).
        votes = [v for v in line.votes.values() if not is_upgrade(line.text, v.text)]
        best = max(votes, key=lambda vote: vote.value)
        # (Scores are sums of fractions: a margin met exactly must not fail on rounding.)
        if best is current:  # the same words, other punctuation
            better = best.score - line.score > self.correct_margin - 1e-9
        else:
            better = best.value - current.value > self.correct_margin - 1e-9
        return self._replace(line, best.text, best.score) if better else None

    @staticmethod
    def _replace(line: _Line, text: str, score: float) -> LineUpgraded:
        before = line.full
        line.text, line.score = text, score
        line.votes.setdefault(key(text), _Vote(text, score, 1))
        return LineUpgraded(line.line_id, line.full, previous=before)

    def _confirm(self, candidate: _Candidate, row: int, now: float, matched: set[int]) -> Event:
        text = candidate.text
        back = self._came_back(text, now)
        if back is not None:  # it left a moment ago and came back
            self._gone.remove(back)
            self._show(back, row, now, matched)
            return LineConfirmed(back.line_id, back.full, repeat=True)
        merged = self._merge_target(text, now, matched)
        if merged is not None:
            (self._gone if merged in self._gone else self._lines).remove(merged)
            before = merged.full
            merged.pieces.append(merged.text)
            merged.text, merged.score, merged.votes = text, candidate.score, {}
            self._show(merged, row, now, matched)
            return LineUpgraded(merged.line_id, merged.full, previous=before)
        line = _Line(self._next_id, text, candidate.score)
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
        return line if ok else None

    def _came_back(self, text: str, now: float) -> _Line | None:
        """The line that left a moment ago and reads like ``text``, if any."""
        return next(
            (
                g
                for g in reversed(self._gone)
                if now - g.seen <= RECENT_S and self._similar(text, g.text)
            ),
            None,
        )

    def _outcome(self, text: str, now: float, matched: set[int]) -> str | None:
        """What confirming ``text`` now would send for translation (see ``_confirm``)."""
        if self._came_back(text, now) is not None:
            return None
        merged = self._merge_target(text, now, matched)
        return text if merged is None else f"{merged.full} {text}"

    def _show(self, line: _Line, row: int, now: float, matched: set[int]) -> None:
        line.row, line.misses, line.seen, line.upgrade = row, 0, now, None
        line.box = self._boxes[row] if row < len(self._boxes) else None
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

    def _score(self, text: str) -> float:
        return self.scorer(text) if self.scorer is not None else 0.0

    def _similar(self, a: str, b: str) -> bool:
        return fuzz.ratio(key(a), key(b)) >= self.similarity
