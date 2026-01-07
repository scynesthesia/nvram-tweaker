import nvram_parsing
from nvram_structures import OptionField, ValueField


def test_find_blocks_populates_fields() -> None:
    sample = (
        "Setup Question = Example\n"
        "Value = <5>\n"
        "Options =*[00]Off\n"
        "Options =[01]On\n"
    )

    blocks, trailing_text = nvram_parsing.find_blocks(sample)

    assert len(blocks) == 1
    fields = blocks[0].fields
    assert any(isinstance(field, ValueField) and field.value == 5 for field in fields)
    option_fields = [field for field in fields if isinstance(field, OptionField)]
    assert len(option_fields) == 2
    assert option_fields[0].selected is True
    assert option_fields[1].selected is False
    assert trailing_text == ""


def test_find_blocks_parses_hex_value() -> None:
    sample = "Setup Question = HexValue\nValue = <0x1f>\n"

    blocks, trailing_text = nvram_parsing.find_blocks(sample)

    assert len(blocks) == 1
    fields = blocks[0].fields
    assert any(isinstance(field, ValueField) and field.value == 31 for field in fields)
    assert trailing_text == ""
