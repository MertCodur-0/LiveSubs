import pytest

from livesubs.engine.text.quality import ReadScorer, penalties
from livesubs.platform.spellcheck import SpellChecker, open_spell_checker

# Misreads of real subtitles seen in game footage (2026-11-14), next to the right read.
PAIRS = [
    ("Heyl Get outl Nowl", "Hey! Get out! Now!"),
    ("Yeah0 Damn", "Yeah? Damn."),
    (
        "I Icy! Stop' Tho government s stealing my gas!",
        "Hey! Stop! The government's stealing my gas!",
    ),
    ("You know ncxt you II bc tolling me", "You know, next you'll be telling me"),
    ("Pio-ethe weapon away.';u•onlt•ayat again:", "Put the weapon away... I won't say it again."),
]
UNKNOWN = {"Heyl", "outl", "Nowl", "Tho", "ncxt", "bc", "Pio-ethe", "u•onlt•ayat"}


def fake_misspelled(text: str) -> list[str]:
    return [word for word in text.replace("!", " ").split() if word.strip(".,?'") in UNKNOWN]


@pytest.mark.parametrize(("garbled", "right"), PAIRS)
def test_the_right_read_scores_higher(garbled: str, right: str) -> None:
    scorer = ReadScorer(fake_misspelled)
    assert scorer.score(right) > scorer.score(garbled)
    assert scorer.best([garbled, right]) == right
    assert scorer.best([right, garbled]) == right


def test_a_complete_read_beats_a_partial_clean_one() -> None:
    scorer = ReadScorer(fake_misspelled)
    assert scorer.best(["you can", "Well maybe, maybe you can just go right ahead and"]).startswith(
        "Well"
    )


def test_ties_keep_the_first_read() -> None:
    scorer = ReadScorer()
    assert scorer.best(["Hello there", "Hello there."]) == "Hello there"
    assert scorer.best([]) == ""


def test_penalty_rules() -> None:
    assert penalties("Hey! Get out! Now!") == (0, 4)
    assert penalties("Yeah0 Damn")[0] == 1  # letters mixed with digits
    assert penalties("government s stealing")[0] == 1  # a stray letter
    assert penalties("I am a man, O Lord")[0] == 0  # real one-letter words
    assert penalties("Stop' now")[0] == 0.5  # a misread "!"
    assert penalties("playin' with the boys'")[0] == 0  # dropped g, plural possessive
    assert penalties("go • now ©")[0] == 2  # odd symbols
    assert penalties("I'ma go", misspelled=["I'ma"])[0] == pytest.approx(0.3)  # slang
    assert penalties("Heyl go", misspelled=["Heyl"])[0] == 1


def test_without_a_dictionary_only_the_shape_rules_count() -> None:
    scorer = ReadScorer()
    assert scorer.score("Heyl Get outl Nowl") == scorer.score("Hey Get out Now")
    assert scorer.score("Yeah0") < scorer.score("Yeah?")


@pytest.fixture(scope="module")
def checker() -> SpellChecker:
    found = open_spell_checker()
    if found is None:
        pytest.skip("Windows has no en-US spell checker here")
    return found


def test_windows_spell_checker_flags_misreads(checker: SpellChecker) -> None:
    assert checker.misspelled("Hey! Get out! Now!") == []
    assert checker.misspelled("Heyl Get outl Nowl") == ["Heyl", "outl", "Nowl"]
    assert checker.misspelled("") == []
    assert checker.misspelled("   ") == []


def test_windows_spell_checker_ranks_real_misreads(checker: SpellChecker) -> None:
    scorer = ReadScorer(checker.misspelled)
    for garbled, right in PAIRS:
        assert scorer.best([garbled, right]) == right, garbled
