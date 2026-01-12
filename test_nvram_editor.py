import logging
from pathlib import Path

import pytest

import nvram_crc
import nvram_editor
import nvram_parsing
import nvram_reconstruction


def test_find_blocks_parses_help_and_missing_help() -> None:
    sample = """Setup Question = WithHelp
//Help String = First help entry
[00]Off
[01]*On

   Setup Question = SpacedHelp
;   Help    String=Second help entry
Options =*[00]Disabled
Options =[01]Enabled

Setup Question = NoHelpHere
Value = <2>
"""

    blocks, trailing_text = nvram_parsing.find_blocks(sample)

    assert [block.name for block in blocks] == ["WithHelp", "SpacedHelp", "NoHelpHere"]
    assert blocks[0].help_string == "First help entry"
    assert blocks[1].help_string == "Second help entry"
    assert blocks[2].help_string is None
    assert trailing_text == ""


def test_warns_on_unparsable_setup_block(caplog) -> None:
    malformed = """Setup Question = FirstValid
Value = <1>

Setup Question Missing Equals
Value = <2>

Setup Question = SecondValid
Value = <3>
"""

    with caplog.at_level(logging.WARNING, logger=nvram_editor.__name__):
        blocks, trailing_text = nvram_parsing.find_blocks(malformed)

    assert [block.name for block in blocks] == ["FirstValid", "SecondValid"]
    assert any("Skipped potential Setup Question block" in record.message for record in caplog.records)
    assert trailing_text == ""


def test_parse_and_rebuild_round_trip_preserves_formatting() -> None:
    mixed_format = (
        "Preamble line\n"
        "# Leading comment\n"
        "  Setup Question = First // inline comment\n"
        "Value = <1>\n"
        "\n"
        "\tSetup Question = Second    ; spaced inline\n"
        "Options =*[00]Enabled\r\n"
        "Options =[01]Disabled\r\n"
        "\r\n"
        ";Trailing separator\n"
        "Setup Question = Third\t# hash inline\n"
        "Token = 0x1234\n"
    )

    blocks, trailing_text = nvram_parsing.find_blocks(mixed_format)
    rebuilt = nvram_reconstruction.rebuild_text(blocks, trailing_text=trailing_text)

    assert [block.name for block in blocks] == ["First", "Second", "Third"]
    assert rebuilt == mixed_format


def test_crc_bypass_modes_apply_expected_changes() -> None:
    sample_content = "HIICrc32=live,ver=02\nSetup Question = Test\nValue = <1>\n"
    placeholder = nvram_crc.recalculate_crc(
        sample_content, "HIICrc32=original,ver=01", bypass=True, bypass_mode="placeholder", tool_version="1.0"
    )
    first_line = placeholder.splitlines()[0]
    assert "<bypassed by nvram-tweaker" in first_line
    assert first_line.endswith(",ver=02")

    emptied = nvram_crc.recalculate_crc(sample_content, "HIICrc32=original,ver=01", bypass=True, bypass_mode="empty")
    assert emptied.splitlines()[0] == "HIICrc32="

    removed = nvram_crc.recalculate_crc(sample_content, "HIICrc32=original,ver=01", bypass=True, bypass_mode="remove")
    assert not removed.startswith("HIICrc32=")


def test_save_blocks_unsafe_crc_bypass_by_default(tmp_path: Path) -> None:
    content = "HIICrc32=abcd\nSetup Question = First\nValue = <1>\n"
    path = tmp_path / "nvram.txt"
    path.write_text(content)

    manager = nvram_editor.NVRAMManager().load_file(path)
    new_text = manager.rebuild_text()

    with pytest.raises(ValueError, match="Unsafe CRC bypass blocked"):
        manager.save(path, new_text, bypass_crc=True, bypass_crc_mode="remove")

    assert path.read_text() == content


def test_save_allows_forced_crc_bypass(tmp_path: Path) -> None:
    content = "HIICrc32=abcd\nSetup Question = First\nValue = <1>\n"
    path = tmp_path / "nvram.txt"
    path.write_text(content)

    manager = nvram_editor.NVRAMManager().load_file(path)
    new_text = manager.rebuild_text()

    manager.save(
        path,
        new_text,
        bypass_crc=True,
        bypass_crc_mode="empty",
        force_crc_bypass=True,
    )

    updated = path.read_text().splitlines()
    assert updated[0] == "HIICrc32="
    assert (tmp_path / "nvram.txt.bak").exists()


def test_save_preserves_batch_edits(tmp_path: Path) -> None:
    original = (
        "Setup Question = First\n"
        "Value = <1>\n"
        "\n"
        "Setup Question = Second\n"
        "Options =*[00]Disabled\n"
        "Options =[01]Enabled\n"
    )
    path = tmp_path / "nvram.txt"
    path.write_text(original)

    manager = nvram_editor.NVRAMManager().load_file(path)
    blocks = manager.blocks

    updated_blocks = nvram_editor.apply_changes(manager, blocks, [0], None, 42)
    updated_blocks = nvram_editor.apply_changes(manager, updated_blocks, [1], "Enabled", None)
    manager.blocks = updated_blocks
    new_text = manager.rebuild_text(updated_blocks)

    manager.save(path, new_text)

    saved_text = path.read_text()
    assert "Value = <42>" in saved_text
    assert "*[01]Enabled" in saved_text
    assert (tmp_path / "nvram.txt.bak").exists()


def test_save_accepts_crlf_without_false_change_detection(tmp_path: Path) -> None:
    original = (
        "Setup Question = IOMMU\r\n"
        "Value = <1>\r\n"
    )
    path = tmp_path / "nvram.txt"
    path.write_bytes(original.encode("utf-8"))

    manager = nvram_editor.NVRAMManager().load_file(path)
    updated_blocks = nvram_editor.apply_changes(manager, manager.blocks, [0], None, 0)
    manager.blocks = updated_blocks
    new_text = manager.rebuild_text(updated_blocks)

    manager.save(path, new_text)
    saved_text = path.read_bytes().decode("utf-8")
    assert "Value = <0>" in saved_text
    assert "\r\n" in saved_text


def test_apply_changes_rejects_out_of_range_value(tmp_path: Path) -> None:
    content = (
        "Setup Question = Range\n"
        "Min = 0\n"
        "Max = 5\n"
        "Value = <3>\n"
    )
    path = tmp_path / "nvram.txt"
    path.write_text(content)

    manager = nvram_editor.NVRAMManager().load_file(path)
    with pytest.raises(ValueError, match="above Max"):
        nvram_editor.apply_changes(manager, manager.blocks, [0], None, 9)


@pytest.mark.parametrize(
    "raw,expected,conversion_note",
    [
        ("10", 10, None),
        ("0x1A", 26, "Entered hex value 0x1A converted to decimal 26."),
        ("+0x2f", 47, "Entered hex value +0x2f converted to decimal 47."),
        ("-10", -10, None),
    ],
)
def test_validate_numeric_input_accepts_valid_values(raw: str, expected: int, conversion_note: str) -> None:
    parsed, note = nvram_editor.validate_numeric_input(raw)
    assert parsed == expected
    assert note == conversion_note


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "0xZZ", "12.5", "0x1g", "123\n", "456\t"],
)
def test_validate_numeric_input_rejects_invalid_values(raw: str) -> None:
    with pytest.raises(ValueError):
        nvram_editor.validate_numeric_input(raw)
