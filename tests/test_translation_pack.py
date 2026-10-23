import json
from pathlib import Path

import pytest

from livesubs.core.translation_pack import PackError, load_pack, parse_pack


def test_pack_fills_the_translation_settings() -> None:
    pack = parse_pack(
        json.dumps(
            {
                "game_context": "  Set in Vice City.  ",
                "glossary": [{"en": "Vice City", "tr": "Vice City"}, {"en": " ", "tr": "x"}],
                "examples": {"Copy.": "Anlaşıldı."},
                "test_lines": [{"en": "Lucia..."}, {"en": "Lucia..."}, "Bad luck, I guess."],
            }
        )
    )
    assert pack.translation.context == "Set in Vice City."
    assert pack.translation.glossary == {"Vice City": "Vice City"}  # empty rows dropped
    assert pack.translation.examples == {"Copy.": "Anlaşıldı."}
    assert pack.test_lines == ("Lucia...", "Bad luck, I guess.")  # duplicates dropped


def test_every_part_is_optional() -> None:
    pack = parse_pack("{}")
    assert pack.translation.context == ""
    assert pack.translation.glossary == {} and pack.translation.examples == {}
    assert pack.test_lines == ()


@pytest.mark.parametrize(
    "text",
    [
        "{nope",
        "[]",
        '{"game_context": 3}',
        '{"glossary": "Vice City"}',
        '{"examples": [["Copy.", "Anlaşıldı."]]}',
        '{"examples": [{"en": "Copy."}]}',
        '{"test_lines": [{"tr": "x"}]}',
    ],
)
def test_broken_packs_are_refused(text: str) -> None:
    with pytest.raises(PackError):
        parse_pack(text)


def test_load_pack_reads_utf8_with_or_without_bom(tmp_path: Path) -> None:
    path = tmp_path / "Çeviri paketi.json"
    path.write_text('{"examples": {"Copy.": "Anlaşıldı."}}', encoding="utf-8-sig")
    assert load_pack(path).translation.examples == {"Copy.": "Anlaşıldı."}
    with pytest.raises(PackError):
        load_pack(tmp_path / "missing.json")
