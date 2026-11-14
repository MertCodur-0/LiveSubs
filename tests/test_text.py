import pytest

from livesubs.engine.text import filters, sentences
from livesubs.engine.text.filters import Reject
from livesubs.engine.text.normalize import clean, key
from livesubs.engine.text.quality import ReadScorer
from livesubs.engine.text.stabilizer import (
    MERGE_GAP_S,
    LineConfirmed,
    LineUpgraded,
    Row,
    ScreenChanged,
    Stabilizer,
    group_rows,
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


def read(
    stab: Stabilizer,
    clock: Clock,
    *rows: str,
    change: Change = Change.SAME,
    pixels: int = 1000,
    after: float = 0.06,
) -> list:
    """One frame: the change verdict, then (when the stabilizer asks) the OCR rows."""
    decision = stab.update(change, pixels, clock(after))
    events = list(decision.events)
    if decision.read or change in (Change.APPEARED, Change.CHANGED):
        events += stab.on_read(list(rows), clock())
    return events


def appear_and_confirm(stab: Stabilizer, clock: Clock, text: str, pixels: int = 1000) -> None:
    assert stab.update(Change.APPEARED, pixels, clock()).read
    assert stab.on_read([text], clock()) == []
    assert stab.update(Change.SAME, pixels, clock(0.06)).read
    events = stab.on_read([text], clock())
    assert events[0] == LineConfirmed(events[0].line_id, text)
    assert isinstance(events[-1], ScreenChanged)


def test_line_needs_two_agreeing_reads(clock: Clock) -> None:
    stab = Stabilizer()
    assert read(stab, clock, LINE, change=Change.APPEARED) == []
    assert read(stab, clock, LINE) == [LineConfirmed(1, LINE), ScreenChanged((1,))]
    assert stab.current == [LINE]


def test_agreement_is_fuzzy_and_the_latest_read_wins(clock: Clock) -> None:
    stab = Stabilizer()
    read(stab, clock, "Look what the law chased in", change=Change.APPEARED)
    events = read(stab, clock, "Look what the law chased in.")
    assert events[0] == LineConfirmed(1, "Look what the law chased in.")


def test_disagreeing_reads_restart_the_count(clock: Clock) -> None:
    stab = Stabilizer()
    assert read(stab, clock, "Look what", change=Change.APPEARED) == []  # still fading in
    assert read(stab, clock, "Look what the law chased in.") == []
    events = read(stab, clock, "Look what the law chased in.")
    assert events[0] == LineConfirmed(1, "Look what the law chased in.")


def test_fast_reread_after_a_candidate(clock: Clock) -> None:
    stab = Stabilizer()
    stab.update(Change.APPEARED, 1000, clock())
    stab.on_read([LINE], clock())
    assert stab.awaiting_confirmation
    assert not stab.update(Change.SAME, 1000, clock(0.02)).read
    assert stab.update(Change.SAME, 1000, clock(0.04)).read


def test_rejected_reads_poll_slowly_unless_glyphs_appear(clock: Clock) -> None:
    stab = Stabilizer()
    stab.update(Change.APPEARED, 200, clock())
    assert stab.on_read(["||||"], clock()) == []
    assert not stab.update(Change.SAME, 200, clock(0.1)).read  # no hurry for junk
    assert stab.update(Change.SAME, 400, clock(0.01)).read  # the line is fading in
    assert stab.update(Change.SAME, 200, clock(0.5)).read  # or the slow poll is due


def test_clear_reports_only_a_screen_that_had_lines(clock: Clock) -> None:
    stab = Stabilizer()
    assert stab.update(Change.NONE, 0, clock()).events == ()
    assert stab.update(Change.CLEARED, 0, clock()).events == ()
    appear_and_confirm(stab, clock, LINE)
    decision = stab.update(Change.CLEARED, 0, clock(1.0))
    assert decision == type(decision)(read=False, events=(ScreenChanged(()),))
    assert stab.current == []


def test_confirmed_line_is_reread_slowly_or_when_it_grows(clock: Clock) -> None:
    stab = Stabilizer()
    appear_and_confirm(stab, clock, PARTIAL, pixels=1000)
    assert not stab.update(Change.SAME, 1000, clock(0.2)).read
    assert stab.update(Change.SAME, 1200, clock(0.01)).read  # 20 % more text pixels
    stab.on_read([PARTIAL], clock())
    assert not stab.update(Change.SAME, 1200, clock(0.2)).read
    assert stab.update(Change.SAME, 1200, clock(0.31)).read


def test_better_read_upgrades_the_line_once_confirmed(clock: Clock) -> None:
    stab = Stabilizer()
    appear_and_confirm(stab, clock, PARTIAL)
    stab.update(Change.SAME, 1300, clock(0.1))
    assert stab.on_read([LINE], clock()) == []  # one read is not enough
    assert stab.update(Change.SAME, 1300, clock(0.06)).read  # confirm it quickly
    assert stab.on_read([LINE], clock()) == [LineUpgraded(1, LINE, previous=PARTIAL)]
    assert stab.current == [LINE]


def test_variants_never_replace_the_confirmed_line(clock: Clock) -> None:
    stab = Stabilizer()
    appear_and_confirm(stab, clock, LINE)
    for variant in (PARTIAL, "Who is this guy? You knew, besides Brain's old cellmate.", LINE):
        assert read(stab, clock, variant, after=0.6) == []
    assert stab.current == [LINE]


GARBLED = "Shitl my truck0 M-my gas!"  # letters mixed with digits: a misread
CLEAN = "Shit, my truck! M-my gas!"


def test_with_a_scorer_the_cleanest_agreeing_read_is_confirmed(clock: Clock) -> None:
    stab = Stabilizer(scorer=ReadScorer().score)
    assert read(stab, clock, CLEAN, change=Change.APPEARED) == []
    assert read(stab, clock, GARBLED)[0] == LineConfirmed(1, CLEAN)  # not the latest read


def test_a_clearly_cleaner_read_corrects_the_confirmed_line(clock: Clock) -> None:
    """OCR garbles a line differently frame to frame; the line converges on its best read."""
    stab = Stabilizer(scorer=ReadScorer().score)
    appear_and_confirm(stab, clock, GARBLED)
    assert read(stab, clock, CLEAN, after=0.6) == [LineUpgraded(1, CLEAN, previous=GARBLED)]
    assert read(stab, clock, GARBLED, after=0.6) == []  # never back to a worse read
    assert stab.current == [CLEAN]


def test_reads_scoring_alike_never_flicker(clock: Clock) -> None:
    stab = Stabilizer(scorer=ReadScorer().score)
    appear_and_confirm(stab, clock, LINE)
    for variant in ("Who is this guy? You knew, besides Brain's old cellmate.", LINE):
        assert read(stab, clock, variant, after=0.6) == []
    assert stab.current == [LINE]


def test_a_cleaner_read_after_a_mask_change_corrects_the_line(clock: Clock) -> None:
    stab = Stabilizer(scorer=ReadScorer().score)
    appear_and_confirm(stab, clock, GARBLED)
    events = read(stab, clock, CLEAN, change=Change.CHANGED, after=0.3)
    assert events == [LineUpgraded(1, CLEAN, previous=GARBLED)]


def test_each_stacked_line_converges_on_its_own_cleanest_read(clock: Clock) -> None:
    other = "Step out of the vehicle!"
    stab = Stabilizer(scorer=ReadScorer().score)
    read(stab, clock, GARBLED, other, change=Change.APPEARED)
    assert read(stab, clock, GARBLED, other)[:2] == [
        LineConfirmed(1, GARBLED),
        LineConfirmed(2, other),
    ]
    assert read(stab, clock, CLEAN, other, after=0.6) == [LineUpgraded(1, CLEAN, previous=GARBLED)]


def test_replaced_line_stays_until_the_new_one_is_confirmed(clock: Clock) -> None:
    stab = Stabilizer()
    appear_and_confirm(stab, clock, LINE)
    assert read(stab, clock, "Look what the law chased in.", change=Change.CHANGED, after=2) == []
    assert stab.current == [LINE]
    assert read(stab, clock, "Look what the law chased in.") == [
        LineConfirmed(2, "Look what the law chased in."),
        ScreenChanged((2,)),
    ]


def test_short_line_is_confirmed_while_its_mask_keeps_changing(clock: Clock) -> None:
    """User report: "I knew Fisk" was on screen, clearly readable, then "wouldn't go quietly."
    replaced it, and only the second part was ever translated. A short line's mask is small,
    so a few pixels of moving background push the thumbnail IoU under the threshold: every
    frame came as CHANGED, which used to throw the candidate away before its confirming read."""
    stab = Stabilizer(merge_split_sentences=False)
    appear_and_confirm(stab, clock, LINE)
    events = []
    for _ in range(6):
        events += read(stab, clock, "I knew Fisk", change=Change.CHANGED, pixels=300, after=0.1)
    # Confirmed once: later reads of the same text are the same line, whatever the mask says.
    assert [e for e in events if isinstance(e, LineConfirmed)] == [LineConfirmed(2, "I knew Fisk")]
    read(stab, clock, "wouldn't go quietly.", change=Change.CHANGED, pixels=600, after=0.1)
    assert read(stab, clock, "wouldn't go quietly.", pixels=600)[0] == LineConfirmed(
        3, "wouldn't go quietly."
    )


def test_line_reappearing_after_a_flicker_is_a_repeat(clock: Clock) -> None:
    stab = Stabilizer()
    appear_and_confirm(stab, clock, LINE)
    stab.update(Change.CLEARED, 0, clock(0.1))  # a white flash hid the subtitle
    read(stab, clock, LINE, change=Change.APPEARED, after=0.1)
    assert read(stab, clock, LINE) == [LineConfirmed(1, LINE, repeat=True), ScreenChanged((1,))]


# --- several lines at once (people talking over each other) -------------------------------------


def test_stacked_lines_are_confirmed_one_by_one_and_scroll_up(clock: Clock) -> None:
    """A game stacks the speakers' lines, the newest at the bottom; old ones scroll up and out.
    Each line is confirmed (and so translated) once, alone, and keeps its id while it moves."""
    a, b, c = "Police! Stop right there!", "Step out of the vehicle!", "Don't play dumb!"
    stab = Stabilizer()
    events = read(stab, clock, a, change=Change.APPEARED) + read(stab, clock, a)
    events += read(stab, clock, a, b, change=Change.CHANGED, after=0.5) + read(stab, clock, a, b)
    events += read(stab, clock, a, b, c, change=Change.CHANGED, after=0.5)
    events += read(stab, clock, a, b, c)
    events += read(stab, clock, b, c, change=Change.CHANGED, after=0.5) + read(stab, clock, b, c)
    events += read(stab, clock, c, change=Change.CHANGED, after=0.5) + read(stab, clock, c)
    events += read(stab, clock, change=Change.CHANGED, after=0.5)  # region empty, but the mask
    events += read(stab, clock)  # still shows something (junk): two empty reads end it
    assert events == [
        LineConfirmed(1, a),
        ScreenChanged((1,)),
        LineConfirmed(2, b),
        ScreenChanged((1, 2)),
        LineConfirmed(3, c),
        ScreenChanged((1, 2, 3)),
        ScreenChanged((2, 3)),
        ScreenChanged((3,)),
        ScreenChanged(()),
    ]


def test_a_new_line_under_a_known_one_is_not_mistaken_for_it(clock: Clock) -> None:
    stab = Stabilizer()
    long = "No way, I'm not doing that again."
    appear_and_confirm(stab, clock, long)
    read(stab, clock, long, "No.", change=Change.CHANGED)
    assert read(stab, clock, long, "No.") == [LineConfirmed(2, "No."), ScreenChanged((1, 2))]


def test_a_short_line_is_never_upgraded_into_the_next_line(clock: Clock) -> None:
    stab = Stabilizer()
    appear_and_confirm(stab, clock, "Yeah.")
    read(stab, clock, "Yeah, I know what you mean.", change=Change.CHANGED)
    events = read(stab, clock, "Yeah, I know what you mean.")
    assert events[0] == LineConfirmed(2, "Yeah, I know what you mean.")


def test_a_wrapped_sentence_is_one_line(clock: Clock) -> None:
    top, bottom = "Excuse me, sir. Sir. Owner of the pickup,", "gas needs to go in the tank."
    stab = Stabilizer()
    read(stab, clock, top, bottom, change=Change.APPEARED)
    assert read(stab, clock, top, bottom) == [
        LineConfirmed(1, f"{top} {bottom}"),
        ScreenChanged((1,)),
    ]
    # Its top row scrolled out (or was lost): what is left is still that line.
    assert read(stab, clock, bottom, change=Change.CHANGED) == []
    assert read(stab, clock, bottom) == []
    assert stab.current == [f"{top} {bottom}"]


@pytest.mark.parametrize(
    ("rows", "lines"),
    [
        (["Police! Stop right there!", "Step out of the vehicle!"], 2),  # two speakers
        (["Hey. Got this Raul guy's car and", "heading to the address he gave."], 1),  # wrapped
        (["Who is this guy?", "know, besides Brian's old cellmate."], 1),  # "You" lost
        (["We have to find", "Lucia before they do."], 1),  # open end, a name below
        (["I'm a hugger.", "Come in here!"], 2),
        (["NO PUNCTUATION HERE", "AND NONE HERE"], 1),  # such games keep one subtitle
        ([], 0),
    ],
)
def test_group_rows(rows: list[str], lines: int) -> None:
    assert len(group_rows(rows)) == lines


def test_a_row_off_to_the_side_never_joins_a_sentence() -> None:
    """A HUD label or a watermark has no sentence end; if it joined the row below, every
    subtitle under it became "LABEL What?" and short lines vanished into it."""
    label, line = Row("TRADE $CYBERLEEK", 0.2), Row("What?", 0.5)
    assert group_rows([label, line]) == ["TRADE $CYBERLEEK", "What?"]
    wrapped = [Row("Hey. Got this Raul guy's car and", 0.5), Row("heading to the address.", 0.52)]
    assert len(group_rows(wrapped)) == 1


def test_short_lines_under_a_label_are_not_swallowed(clock: Clock) -> None:
    stab = Stabilizer()
    appear_and_confirm(stab, clock, "TRADE CYBERLEEK")
    read(stab, clock, "TRADE CYBERLEEK What?", change=Change.CHANGED)
    events = read(stab, clock, "TRADE CYBERLEEK What?")
    assert events[0] == LineConfirmed(2, "TRADE CYBERLEEK What?")


def test_a_sentence_split_over_two_subtitles_is_merged(clock: Clock) -> None:
    stab = Stabilizer()
    appear_and_confirm(stab, clock, "I knew Fisk")
    read(stab, clock, "wouldn't go quietly.", change=Change.CHANGED, after=0.8)
    assert read(stab, clock, "wouldn't go quietly.") == [
        LineUpgraded(1, "I knew Fisk wouldn't go quietly.", previous="I knew Fisk"),
    ]  # the same line on screen (id 1): no ScreenChanged
    assert stab.current == ["wouldn't go quietly."]


def test_pieces_too_far_apart_are_not_merged(clock: Clock) -> None:
    stab = Stabilizer()
    appear_and_confirm(stab, clock, "I knew Fisk")
    stab.update(Change.CLEARED, 0, clock(0.1))
    read(stab, clock, "wouldn't go quietly.", change=Change.APPEARED, after=MERGE_GAP_S + 0.1)
    events = read(stab, clock, "wouldn't go quietly.")
    assert events[0] == LineConfirmed(2, "wouldn't go quietly.")


def test_a_continuation_under_its_line_upgrades_it(clock: Clock) -> None:
    """The next piece shows under the first one, both on screen: one sentence, one line."""
    stab = Stabilizer()
    appear_and_confirm(stab, clock, "I knew Fisk")
    read(stab, clock, "I knew Fisk", "wouldn't go quietly.", change=Change.CHANGED)
    assert read(stab, clock, "I knew Fisk", "wouldn't go quietly.") == [
        LineUpgraded(1, "I knew Fisk wouldn't go quietly.", previous="I knew Fisk"),
    ]


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
