"""Which translations the overlay shows while the game's lines come, stack and scroll out."""

from livesubs.ui.shown_lines import ShownLine, ShownLines


def texts(shown: ShownLines, now: float) -> list[str]:
    return [line.translation for line in shown.lines(now)]


def test_lines_on_screen_show_in_the_games_order_once_translated() -> None:
    shown = ShownLines(clear_delay_s=2.0)
    shown.screen((1, 2), 0.0)
    assert texts(shown, 0.0) == []
    shown.translated(2, "İki", "Two", 0.5)
    shown.translated(1, "Bir", "One", 0.6)
    assert shown.lines(0.6) == [ShownLine("Bir", "One"), ShownLine("İki", "Two")]


def test_a_line_that_left_stays_for_the_delay() -> None:
    shown = ShownLines(clear_delay_s=2.0)
    shown.screen((1,), 0.0)
    shown.translated(1, "Bir", "", 0.5)
    shown.screen((), 1.0)
    assert texts(shown, 2.9) == ["Bir"]
    assert shown.next_change() == 3.0
    assert texts(shown, 3.0) == []
    assert shown.next_change() is None


def test_a_line_that_left_stays_while_the_next_one_waits_for_its_translation() -> None:
    """One subtitle replaced by the next: the old translation stays until the new one's comes,
    however long the model takes (a blank overlay looked like subtitles vanishing early)."""
    shown = ShownLines(clear_delay_s=2.0)
    shown.screen((1,), 0.0)
    shown.translated(1, "Bir", "", 0.5)
    shown.screen((2,), 1.0)
    assert shown.next_change() is None
    assert texts(shown, 9.0) == ["Bir"]
    shown.translated(2, "İki", "", 9.5)
    assert texts(shown, 9.5) == ["İki"]


def test_a_held_line_gets_its_full_delay_when_the_waiting_line_leaves_untranslated() -> None:
    shown = ShownLines(clear_delay_s=2.0)
    shown.screen((1,), 0.0)
    shown.translated(1, "Bir", "", 0.5)
    shown.screen((2,), 1.0)
    shown.screen((), 5.0)  # line 2 left before its translation came
    assert texts(shown, 6.9) == ["Bir"]
    assert texts(shown, 7.0) == []


def test_a_newer_translation_pushes_out_the_lines_that_left_before_it() -> None:
    shown = ShownLines(clear_delay_s=2.0)
    shown.screen((1, 2), 0.0)
    shown.translated(1, "Bir", "", 0.1)
    shown.translated(2, "İki", "", 0.2)
    shown.screen((2, 3), 1.0)  # line 1 scrolled out, line 3 came in under line 2
    assert texts(shown, 1.0) == ["Bir", "İki"]
    shown.translated(3, "Üç", "", 1.5)
    assert texts(shown, 1.5) == ["İki", "Üç"]


def test_a_late_translation_shows_for_the_delay() -> None:
    shown = ShownLines(clear_delay_s=2.0)
    shown.screen((1,), 0.0)
    shown.screen((), 0.5)  # gone before the model answered
    shown.translated(1, "Bir", "", 1.0)
    assert texts(shown, 2.9) == ["Bir"]
    assert texts(shown, 3.1) == []


def test_a_line_back_on_screen_shows_again() -> None:
    shown = ShownLines(clear_delay_s=0.5)
    shown.screen((1,), 0.0)
    shown.translated(1, "Bir", "", 0.1)
    shown.screen((), 0.2)
    assert texts(shown, 1.0) == []
    shown.screen((1,), 1.5)  # reappeared after a flash: its id and translation come back
    assert texts(shown, 1.5) == ["Bir"]


def test_reset_and_hide_all() -> None:
    shown = ShownLines()
    shown.screen((1,), 0.0)
    shown.translated(1, "Bir", "", 0.1)
    shown.hide_all()
    assert texts(shown, 0.2) == []
    assert shown.translation(1) == ShownLine("Bir", "")  # kept for a repeat
    shown.reset()
    assert shown.translation(1) is None
