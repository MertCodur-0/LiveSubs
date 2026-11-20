"""Speaker names in front of subtitles ("Lucia: ..."): parsing, row grouping, their colour."""

import cv2
import numpy as np
import pytest

from livesubs.engine.text import speakers
from livesubs.engine.text.stabilizer import Row, group_rows
from livesubs.engine.vision.rows import annotate, name_color

LUCIA = (248, 238, 132)  # BGR of her name in GTA VI (measured on a recording: #84eef8)
WHITE, GREY = (239, 239, 239), (165, 165, 165)


@pytest.mark.parametrize(
    ("text", "speaker", "sentence"),
    [
        ("Lucia: Ugh. I'm aching all over.", "Lucia", "Ugh. I'm aching all over."),
        ("Cal: Hey, barkeep, three beers!", "Cal", "Hey, barkeep, three beers!"),
        ("Officer Diaz: Step out of the car.", "Officer Diaz", "Step out of the car."),
        ("Man 2 : Over here!", "Man 2", "Over here!"),
        ("I'm just saying.", "", "I'm just saying."),
        ("In Spanish: Hola.", "", "In Spanish: Hola."),  # a language note, no one speaks
        ("It's 10:30 already.", "", "It's 10:30 already."),
        ("listen: this is it", "", "listen: this is it"),  # lower case: no name
        ("Lucia:", "", "Lucia:"),  # nothing said
    ],
)
def test_split(text: str, speaker: str, sentence: str) -> None:
    assert speakers.split(text) == (speaker, sentence)


def test_a_named_row_starts_a_line_and_its_rows_follow_it() -> None:
    """GTA VI wraps a speaker's subtitle under the name; the next row starts with a capital
    after a full stop ("I'm just saying.") and used to become a line of its own."""
    rows = [
        Row("Lucia: You know, I was meant for finer things than forced labor.", 0.5, tone=239),
        Row("I'm just saying.", 0.5, tone=239),
        Row("Jason: Mm-hmm.", 0.5, tone=239),
    ]
    assert group_rows(rows) == [
        "Lucia: You know, I was meant for finer things than forced labor. I'm just saying.",
        "Jason: Mm-hmm.",
    ]


def test_a_grey_row_under_a_named_line_is_someone_else() -> None:
    rows = [Row("Cal: Three beers, now", 0.5, tone=239), Row("nice day, huh?", 0.5, tone=165)]
    assert group_rows(rows) == ["Cal: Three beers, now", "nice day, huh?"]


def _row_image(parts: list[tuple[str, tuple[int, int, int]]]) -> np.ndarray:
    image = np.full((60, 900, 3), 25, np.uint8)
    x = 20
    for text, color in parts:
        cv2.putText(image, text, (x, 42), cv2.FONT_HERSHEY_SIMPLEX, 1.1, color, 2)
        x += cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 1.1, 2)[0][0] + 12
    return image


def test_annotate_measures_the_name_colour_and_the_sentence_tone() -> None:
    for sentence_color, tone in ((WHITE, 239), (GREY, 165)):
        image = _row_image([("Lucia:", LUCIA), ("You know, I was meant for more.", sentence_color)])
        letters = (cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) > 100).astype(np.uint8) * 255
        xs = np.nonzero(letters.any(axis=0))[0]
        box = (xs.min() / 900, 0.1, (xs.max() + 1) / 900, 0.9)
        (row,) = annotate([Row("Lucia: You know, I was meant for more.", box=box)], image, letters)
        assert row.tone == pytest.approx(tone, abs=6)
        r, g, b = (int(row.speaker_color[i : i + 2], 16) for i in (1, 3, 5))
        assert abs(r - LUCIA[2]) < 20 and abs(g - LUCIA[1]) < 20 and abs(b - LUCIA[0]) < 20


def test_a_white_name_has_no_colour() -> None:
    assert name_color(_row_image([("Lucia:", WHITE)])) is None
