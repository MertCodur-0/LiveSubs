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


def test_punctuation_glued_between_letters_is_a_misread() -> None:
    scorer = ReadScorer()
    assert scorer.score("Hey! Stop! The government's") > scorer.score("Hey! Stop!h'he government's")
    assert scorer.score("Put the weapon away") > scorer.score("Put the.weapon away")
    assert scorer.score("Go to the engineer") > scorer.score("Go to t)ie engineer")
    # Initialisms, ellipses and hyphenated or contracted words are fine.
    assert penalties("Wait...what, the U.S. at 3 a.m.? Don't. T-Then go")[0] == 3  # 3 lone letters
    assert penalties("Wait...what. Don't. T-Then go")[0] == 0


def test_a_lone_quote_or_bracket_costs_a_little() -> None:
    assert penalties('Another one") You')[0] == 1  # both a lone quote and a lone bracket
    assert penalties('Say "give them a show" (now)')[0] == 0


def test_a_repeated_word_is_not_a_misspelling(checker: SpellChecker) -> None:
    assert checker.misspelled("Here Kitty Kitty!") == []
    assert checker.misspelled("Yes yes, Heyl") == ["Heyl"]
    scorer = ReadScorer(checker.misspelled)
    assert scorer.best(["Here Kitty Kittyl", "Here Kitty Kitty!"]) == "Here Kitty Kitty!"


def test_a_dropped_g_is_a_word(checker: SpellChecker) -> None:
    scorer = ReadScorer(checker.misspelled)
    assert penalties("'cause girl was walkin' funny.", ["walkin"])[0] == 1  # the raw verdict
    assert scorer.score("'cause girl was walkin' funny.") == scorer.score(
        "'cause girl was walking funny."
    )
    assert scorer.best(["'cause girl was walktn' funny.", "'cause girl was walkin' funny."]) == (
        "'cause girl was walkin' funny."
    )
    # Slang and names the dictionary lacks still cost less than a garbled read of the line.
    assert scorer.score("I'ma fuck your ass up!") > scorer.score("I'ma fuck your ass upl")
    assert scorer.score("They don't know nothin' about me") > scorer.score(
        "They don't know nothln' about me"
    )


def test_a_mixed_run_of_punctuation_is_a_misread() -> None:
    assert penalties("left met:. now")[0] == 0.5
    assert penalties("Stop!. The end,. Go")[0] == 1
    assert penalties("Wait... What?! No!! Really?? Hm.")[0] == 0


def test_each_row_is_taken_from_the_input_that_reads_it_cleanest() -> None:
    """Both OCR inputs are read; the cleaner one overall still garbles a row the other gets."""
    import numpy as np

    from livesubs.core.geometry import Rect
    from livesubs.engine.ocr.winrt_ocr import OcrLine, OcrResult
    from livesubs.engine.pipeline import read_subtitle
    from livesubs.engine.vision.preprocess import Preprocessed

    def result(*rows: tuple[str, int]) -> OcrResult:
        return OcrResult(tuple(OcrLine(t, Rect(100, y, 400, 40)) for t, y in rows), 1.0)

    first = result(("Police! Stop right there!", 10), ("Don't play dumbi", 60))
    second = result(("Pol1ce! Stop rlght there!", 12), ("Don't play dumb!", 58))
    image = np.zeros((300, 600), np.uint8)
    processed = Preprocessed(mask=image, ocr_image=image, sharp_image=image.copy(), scale=1.0)

    class FakeOcr:
        def recognize(self, img: np.ndarray) -> OcrResult:
            return first if img is processed.ocr_image else second

    scorer = ReadScorer(lambda text: [w for w in text.split() if w in {"dumbi", "Pol1ce!"}])
    _, _, rows = read_subtitle(FakeOcr(), processed, scorer)  # type: ignore[arg-type]
    assert [row.text for row in rows] == ["Police! Stop right there!", "Don't play dumb!"]


def test_an_unknown_possessive_is_not_slang() -> None:
    assert penalties("Tltnat's them", ["Tltnat's"]) == (0.3, 1)
    assert penalties("I'ma go", ["I'ma"]) == (0.3, 2)


def test_a_hyphenated_word_with_an_unknown_part_is_unknown() -> None:
    assert penalties("Uh-uhr bitch", ["uhr"])[0] == 1
    assert penalties("Uh-uh, bitch. T-Then go", [])[0] == 0
