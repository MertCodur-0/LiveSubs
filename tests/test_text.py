import pytest

from livesubs.engine.text import filters, sentences
from livesubs.engine.text.filters import Reject
from livesubs.engine.text.normalize import clean, key
from livesubs.engine.text.stabilizer import (
    LineCleared,
    LineConfirmed,
    LineUpgraded,
    Stabilizer,
    is_upgrade,
)
from livesubs.engine.vision.change import Change

APOSTROPHE = chr(0x2019)
ELLIPSIS = chr(0x2026)

# --- normalize -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "cleaned"),
    [
        (f"Boobie, huh? Yeah, he ain{APOSTROPHE}t here", "Boobie, huh? Yeah, he ain't here"),
        (f"looks pretty good, so{ELLIPSIS} You wanna", "looks pretty good, so... You wanna"),
        ("  Who is   this\nguy? ", "Who is this guy?"),
        ("| don't even wanna", "I don't even wanna"),
        ("Like, | don't", "Like, I don't"),
        ("l'm a hugger", "I'm a hugger"),
        ("l am here", "I am here"),
        ("get in here! |", "get in here!"),
        ("_ Look what the law chased in. ~", "Look what the law chased in."),
        ("Hello .", "Hello."),
        ("Ill be back", "Ill be back"),  # an "l" inside a word stays
        ("I was going to-", "I was going to-"),  # an interrupted line keeps its dash
    ],
)
def test_clean(raw: str, cleaned: str) -> None:
    assert clean(raw) == cleaned


def test_key_ignores_case_accents_punctuation_and_spacing() -> None:
    assert key("Raymond, listen very closely, cabrón.") == "raymond listen very closely cabron"
    assert key("Who is this guy?") == key("who  is this guy")
    assert key(f"Lori{APOSTROPHE}s Snapmatic") == key("Lori's snapmatic") == "loris snapmatic"


# --- filters -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("", Reject.EMPTY),
        ("   ", Reject.EMPTY),
        ("a", Reject.TOO_SHORT),
        ("- .", Reject.TOO_SHORT),
        ("ok 12/34 :: 56", Reject.FEW_LETTERS),
        ("Hey ||||", Reject.REPEATED_JUNK),
        ("Wait..........", Reject.REPEATED_JUNK),
        ("Wsffz tlkr", Reject.NO_VOWELS),
        ("Go!", None),
        ("No.", None),
        ("Hmm.", None),
        ("Shh!", None),
        ("so... You wanna head out?", None),
        ("Raymond, listen very closely, cabrón.", None),
        ("What-should I tell you, to put it in youir calendar?", None),
    ],
)
def test_filters(text: str, reason: Reject | None) -> None:
    verdict = filters.check(text)
    assert verdict.reason == reason
    assert verdict.ok is (reason is None)


# --- is_upgrade ----------------------------------------------------------------------------------


def test_upgrade_adds_words_and_keeps_the_rest() -> None:
    current = "know, besides Brian's old cellmate."
    assert is_upgrade("Who is this guy? You know, besides Brian's old cellmate.", current)
    assert is_upgrade("You know, besides Brian's old cellmate.", current)


def test_not_an_upgrade() -> None:
    current = "You know, besides Brian's old cellmate."
    assert not is_upgrade(current, current)  # nothing added
    assert not is_upgrade("You knew, besides Brain's old cellmate.", current)  # just a variant
    assert not is_upgrade("Look what the law chased in tonight, bro.", current)  # another line
    assert not is_upgrade("anything", "")


# --- Stabilizer ----------------------------------------------------------------------------------

LINE = "Who is this guy? You know, besides Brian's old cellmate."
PARTIAL = "Who is this guy? know, besides Brian's old cellmate."  # "You" lost on a bright road


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self, advance: float = 0.0) -> float:
        self.now += advance
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


def appear_and_confirm(stab: Stabilizer, clock: Clock, text: str, pixels: int = 1000) -> None:
    assert stab.update(Change.APPEARED, pixels, clock()).read
    assert stab.on_read(text, clock()) is None
    assert stab.update(Change.SAME, pixels, clock(0.06)).read
    assert stab.on_read(text, clock()) == LineConfirmed(text)


def test_line_needs_two_agreeing_reads(clock: Clock) -> None:
    stab = Stabilizer()
    appear_and_confirm(stab, clock, LINE)
    assert stab.current == LINE


def test_agreement_is_fuzzy_and_the_latest_read_wins(clock: Clock) -> None:
    stab = Stabilizer()
    stab.update(Change.APPEARED, 1000, clock())
    stab.on_read("Look what the law chased in", clock())
    stab.update(Change.SAME, 1000, clock(0.06))
    assert stab.on_read("Look what the law chased in.", clock()) == LineConfirmed(
        "Look what the law chased in."
    )


def test_disagreeing_reads_restart_the_count(clock: Clock) -> None:
    stab = Stabilizer()
    stab.update(Change.APPEARED, 1000, clock())
    assert stab.on_read("Look what", clock()) is None  # still fading in
    stab.update(Change.SAME, 1000, clock(0.06))
    assert stab.on_read("Look what the law chased in.", clock()) is None
    stab.update(Change.SAME, 1000, clock(0.06))
    assert stab.on_read("Look what the law chased in.", clock()) == LineConfirmed(
        "Look what the law chased in."
    )


def test_fast_reread_after_a_candidate(clock: Clock) -> None:
    stab = Stabilizer()
    stab.update(Change.APPEARED, 1000, clock())
    stab.on_read(LINE, clock())
    assert not stab.update(Change.SAME, 1000, clock(0.02)).read
    assert stab.update(Change.SAME, 1000, clock(0.04)).read


def test_rejected_reads_poll_slowly_unless_glyphs_appear(clock: Clock) -> None:
    stab = Stabilizer()
    stab.update(Change.APPEARED, 200, clock())
    assert stab.on_read("||||", clock()) is None
    assert not stab.update(Change.SAME, 200, clock(0.1)).read  # no hurry for junk
    assert stab.update(Change.SAME, 400, clock(0.01)).read  # the line is fading in
    assert stab.update(Change.SAME, 200, clock(0.5)).read  # or the slow poll is due


def test_clear_reports_only_a_line_that_existed(clock: Clock) -> None:
    stab = Stabilizer()
    assert stab.update(Change.NONE, 0, clock()).event is None
    assert stab.update(Change.CLEARED, 0, clock()).event is None
    appear_and_confirm(stab, clock, LINE)
    decision = stab.update(Change.CLEARED, 0, clock(1.0))
    assert decision == type(decision)(read=False, event=LineCleared())
    assert stab.current is None


def test_confirmed_line_is_reread_slowly_or_when_it_grows(clock: Clock) -> None:
    stab = Stabilizer()
    appear_and_confirm(stab, clock, PARTIAL, pixels=1000)
    assert not stab.update(Change.SAME, 1000, clock(0.2)).read
    assert stab.update(Change.SAME, 1200, clock(0.01)).read  # 20 % more text pixels
    stab.on_read(PARTIAL, clock())
    assert not stab.update(Change.SAME, 1200, clock(0.2)).read
    assert stab.update(Change.SAME, 1200, clock(0.31)).read


def test_better_read_upgrades_the_line_once_confirmed(clock: Clock) -> None:
    stab = Stabilizer()
    appear_and_confirm(stab, clock, PARTIAL)
    stab.update(Change.SAME, 1300, clock(0.1))
    assert stab.on_read(LINE, clock()) is None  # one read is not enough
    assert stab.update(Change.SAME, 1300, clock(0.06)).read  # confirm it quickly
    assert stab.on_read(LINE, clock()) == LineUpgraded(LINE, previous=PARTIAL)
    assert stab.current == LINE


def test_variants_never_replace_the_confirmed_line(clock: Clock) -> None:
    stab = Stabilizer()
    appear_and_confirm(stab, clock, LINE)
    for variant in (PARTIAL, "Who is this guy? You knew, besides Brain's old cellmate.", LINE):
        stab.update(Change.SAME, 1000, clock(0.6))
        assert stab.on_read(variant, clock()) is None
    assert stab.current == LINE


def test_changed_line_keeps_the_old_one_until_confirmed(clock: Clock) -> None:
    stab = Stabilizer()
    appear_and_confirm(stab, clock, LINE)
    stab.update(Change.CHANGED, 900, clock(2.0))
    assert stab.on_read("Look what the law chased in.", clock()) is None
    assert stab.current == LINE
    stab.update(Change.SAME, 900, clock(0.06))
    assert stab.on_read("Look what the law chased in.", clock()) == LineConfirmed(
        "Look what the law chased in."
    )


def test_short_line_is_confirmed_while_its_mask_keeps_changing(clock: Clock) -> None:
    """User report: "I knew Fisk" was on screen, clearly readable, then "wouldn't go quietly."
    replaced it, and only the second part was ever translated. A short line's mask is small,
    so a few pixels of moving background push the thumbnail IoU under the threshold: every
    frame came as CHANGED, which used to throw the candidate away before its confirming read."""
    stab = Stabilizer()
    appear_and_confirm(stab, clock, LINE)
    events = []
    for _ in range(6):
        assert stab.update(Change.CHANGED, 300, clock(0.1)).read
        events.append(stab.on_read("I knew Fisk", clock()))
    # Confirmed once: later reads of the same text are the same line, whatever the mask says.
    assert [e for e in events if e is not None] == [LineConfirmed("I knew Fisk")]
    stab.update(Change.CHANGED, 600, clock(0.1))
    stab.on_read("wouldn't go quietly.", clock())
    stab.update(Change.SAME, 600, clock(0.06))
    assert stab.on_read("wouldn't go quietly.", clock()) == LineConfirmed("wouldn't go quietly.")


def test_line_reappearing_after_a_flicker_is_a_repeat(clock: Clock) -> None:
    stab = Stabilizer()
    appear_and_confirm(stab, clock, LINE)
    stab.update(Change.CLEARED, 0, clock(0.1))  # a white flash hid the subtitle
    stab.update(Change.APPEARED, 1000, clock(0.1))
    stab.on_read(LINE, clock())
    stab.update(Change.SAME, 1000, clock(0.06))
    assert stab.on_read(LINE, clock()) == LineConfirmed(LINE, repeat=True)


@pytest.mark.parametrize(
    ("previous", "text", "merged"),
    [
        ("I knew Fisk", "wouldn't go quietly.", True),
        ("We need that evidence", "if we want to put him away for good.", True),
        ("Hold on,", "...and he's hitting us with everything.", True),
        ("I knew Fisk.", "wouldn't go quietly.", False),  # sentence ended
        ('He said "Go!"', "and we went.", False),  # ended inside quotes
        ("Location--", "units, report.", False),  # interrupted: another speaker
        ("I knew Fisk", "Wouldn't go quietly.", False),  # capital: a new sentence
        ("I KNEW FISK", "WOULDN'T GO QUIETLY", False),  # all caps never merge
        ("I knew Fisk", "", False),
    ],
)
def test_sentence_continues(previous: str, text: str, merged: bool) -> None:
    assert sentences.continues(previous, text) is merged
