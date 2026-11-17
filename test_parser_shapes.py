"""Parser behaviour on the shapes found in a real SCEWIN export.

Uses ``tests/fixtures/real_export_trimmed.txt`` (see ``test_real_export.py``) plus a
few tiny inline samples for the edge cases the real file does not contain.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import nvram_editor
import nvram_parsing
import nvram_safety
from nvram_reconstruction import rebuild_text
from nvram_structures import ValueField

FIXTURE = Path(__file__).parent / "tests" / "fixtures" / "real_export_trimmed.txt"


@pytest.fixture
def manager(tmp_path: Path) -> nvram_editor.NVRAMManager:
    target = tmp_path / "nvram.txt"
    shutil.copyfile(FIXTURE, target)
    return nvram_editor.NVRAMManager().load_file(target)


def live(manager: nvram_editor.NVRAMManager, name: str):
    return next(b for b in manager.blocks if b.name == name and not b.is_commented)


def index_of(manager: nvram_editor.NVRAMManager, name: str) -> int:
    return next(i for i, b in enumerate(manager.blocks) if b.name == name and not b.is_commented)


def save_edit(manager: nvram_editor.NVRAMManager, name: str, new_value: int) -> bytes:
    idx = index_of(manager, name)
    updated = nvram_editor.apply_changes(manager, manager.blocks, [idx], None, new_value)
    manager.save(manager.path, manager.rebuild_text(updated))
    return manager.path.read_bytes()


# --- question names ---------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "PCIe/GFX Lanes Configuration",
        "SataExpress/M.2 PCIe Switch",
        "Clock Power Management(CLKREQ#)",
        "Boot Option #1",
        "SATA0 Redriver CHA VOD Control: VOD/VID Ratio",
        "Plain Name",
    ],
)
def test_names_are_not_cut_at_slashes_hashes_or_semicolons(name: str) -> None:
    text = f"Setup Question\t= {name}\r\nToken\t=01\r\nValue\t=<1>\r\n"
    blocks, trailing = nvram_parsing.find_blocks(text)
    assert [b.name for b in blocks] == [name]
    assert blocks[0].inline_comment == ""
    assert rebuild_text(blocks, trailing) == text


@pytest.mark.parametrize(
    "header, name, comment",
    [
        ("Setup Question\t= Fan Speed\t// SCEWIN note", "Fan Speed", "\t// SCEWIN note"),
        ("Setup Question = PCIe/GFX Lanes // note", "PCIe/GFX Lanes", " // note"),
    ],
)
def test_real_trailing_comments_are_still_split_off(header: str, name: str, comment: str) -> None:
    text = f"{header}\nValue =<1>\n"
    blocks, trailing = nvram_parsing.find_blocks(text)
    assert blocks[0].name == name and blocks[0].inline_comment == comment
    assert rebuild_text(blocks, trailing) == text


@pytest.mark.parametrize(
    "name",
    ["Second    ; spaced inline", "Third\t# hash inline"],
)
def test_hash_and_semicolon_never_start_a_comment(name: str) -> None:
    """SCEWIN only knows ``//``; anything else stays part of the name, where the checks see it."""

    text = f"Setup Question = {name}\nValue =<1>\n"
    blocks, trailing = nvram_parsing.find_blocks(text)
    assert blocks[0].name == name and blocks[0].inline_comment == ""
    assert rebuild_text(blocks, trailing) == text


# --- commented-out questions ------------------------------------------------
@pytest.mark.parametrize("marker", ["#", ";", "/", "# ", "; ", "/ "])
def test_only_double_slash_comments_a_question_out(marker: str) -> None:
    text = (
        "Setup Question = First\nOptions =*[00]A\n         [01]B\n\n"
        f"{marker}Setup Question = Second\nOptions =[00]A\n         *[01]B\n"
    )
    blocks, trailing = nvram_parsing.find_blocks(text)
    # Not a header: the line stays in the body of the question before it, so nothing is hidden.
    assert [(b.name, b.is_commented) for b in blocks] == [("First", False)]
    assert f"{marker}Setup Question = Second" in blocks[0].body
    assert rebuild_text(blocks, trailing) == text


def test_double_slash_question_is_inactive() -> None:
    blocks, _ = nvram_parsing.find_blocks("//Setup Question = A\n// Value =<1>\n\n// Setup Question = B\n")
    assert [(b.name, b.is_commented) for b in blocks] == [("A", True), ("B", True)]




def test_commented_twin_is_flagged_and_has_no_fields(manager) -> None:
    twins = [b for b in manager.blocks if b.name == "PCIe/GFX Lanes Configuration"]
    assert [b.is_commented for b in twins] == [True, False]
    assert twins[0].fields == []
    assert twins[1].fields  # the live twin keeps its options


def test_commented_question_cannot_be_edited_or_matched(manager) -> None:
    commented = next(b for b in manager.blocks if b.is_commented)
    with pytest.raises(ValueError, match="commented out"):
        manager.update_options(commented, "x8x8")
    with pytest.raises(ValueError, match="commented out"):
        manager.update_value(commented, 1)
    matches = manager.filter_blocks("PCIe/GFX Lanes", exact=False, token=None)
    assert len(matches) == 1 and not matches[0].is_commented


def test_commented_questions_are_ignored_by_safety_checks(manager) -> None:
    text = manager.rebuild_text()
    commented_only = [b for b in manager.blocks if b.is_commented]
    assert nvram_safety.find_risky_blocks(commented_only) == []
    assert nvram_safety.diff_texts(text, text) == []
    assert nvram_safety.preflight_issues(text) == []


def test_comment_prefixed_option_lines_are_not_options() -> None:
    blocks, _ = nvram_parsing.find_blocks("Setup Question = X\nOptions =*[00]A\n//         [01]B\n")
    assert [f.code for f in blocks[0].fields] == ["00"]


def test_commented_value_lines_are_not_values() -> None:
    assert nvram_parsing.VALUE_PATTERN.search("// Value =<1>\n") is None


# --- numeric values: <N> and bare N ----------------------------------------


def test_bare_value_is_parsed_and_edited_in_place(manager) -> None:
    block = live(manager, "SataRedrv Com Option1 Address")
    assert [f.value for f in block.fields if isinstance(f, ValueField)] == [0]
    assert block.width == 1
    original = manager.path.read_bytes()

    saved = save_edit(manager, "SataRedrv Com Option1 Address", 7)

    before, after = original.split(b"\n"), saved.split(b"\n")
    changed = [(a, b) for a, b in zip(before, after) if a != b]
    assert len(before) == len(after) and changed == [(b"Value\t=0\r", b"Value\t=7\r")]


def test_angle_bracket_value_keeps_its_form(manager) -> None:
    original = manager.path.read_bytes()
    saved = save_edit(manager, "Adjust APU VDDP Value", 12)
    changed = [(a, b) for a, b in zip(original.split(b"\n"), saved.split(b"\n")) if a != b]
    assert len(changed) == 1
    assert changed[0][1].startswith(b"Value\t=<12>")


@pytest.mark.parametrize(
    "raw, expected",
    [("08", 8), ("05", 5), ("0", 0), ("0x1F", 31), ("-0x10", -16), ("+7", 7), ("007", 7)],
)
def test_numbers_are_decimal_unless_hex_prefixed(raw: str, expected: int) -> None:
    assert nvram_parsing.validate_numeric_input(raw)[0] == expected


@pytest.mark.parametrize(
    "line, value, base",
    [
        ("Value\t=<30>", 30, 10),  # angle brackets: decimal
        ("Value =<64>", 64, 10),
        ("Value = <1>", 1, 10),
        ("Value\t=0", 0, 16),  # bare: hexadecimal
        ("Value\t=64", 100, 16),
        ("Value\t=3FF", 1023, 16),
        ("Value\t=FF", 255, 16),
        ("Value\t=1A0", 416, 16),
        ("Value\t=FFFFFFFB", 0xFFFFFFFB, 16),
        ("Value\t=0x1A", 26, 16),
        ("Value\t=0\t// Enabled = 1, Disabled = 0", 0, 16),  # trailing comment, as real exports have
    ],
)
@pytest.mark.parametrize("ending", ["\n", "\r\n", "\r\r\n", ""])
def test_value_forms_and_their_base(line: str, value: int, base: int, ending: str) -> None:
    blocks, _ = nvram_parsing.find_blocks(f"Setup Question = Q\r\nWidth\t=04\r\n{line}{ending}")
    assert [(f.value, f.base) for f in blocks[0].fields if isinstance(f, ValueField)] == [(value, base)]
    assert not nvram_parsing.has_unreadable_value(blocks[0].body)


@pytest.mark.parametrize(
    "line",
    ["Value =10 junk", "Value =3 FF", "Value =<3F>", "Value =<30", "Value =12G", "Value =<30>x", 'Value ="text"'],
)
def test_a_value_that_is_not_wholly_a_number_is_never_matched_in_part(line: str) -> None:
    text = f"Setup Question = Q\nWidth =02\n{line}\n"
    blocks, _ = nvram_parsing.find_blocks(text)
    assert not [f for f in blocks[0].fields if isinstance(f, ValueField)]
    assert nvram_parsing.has_unreadable_value(blocks[0].body)
    manager = nvram_editor.NVRAMManager()
    with pytest.raises(ValueError, match="left alone"):
        manager.update_value(blocks[0], 5)


@pytest.mark.parametrize(
    "line, new_value, expected",
    [
        ("Value\t=3FF", 10, "Value\t=A"),  # the audit case: used to become "10FF"
        ("Value\t=FF", 26, "Value\t=1A"),
        ("Value\t=0\t// Enabled = 1, Disabled = 0", 1, "Value\t=1\t// Enabled = 1, Disabled = 0"),
        ("Value\t=<30>", 45, "Value\t=<45>"),
    ],
)
@pytest.mark.parametrize("ending", ["\n", "\r\n", "\r\r\n"])
def test_an_edit_writes_the_number_in_the_base_of_its_line(line: str, new_value: int, expected: str, ending: str) -> None:
    tail = f"{ending}{ending}Setup Question = Next{ending}Value =<1>{ending}"
    text = f"Setup Question = Q{ending}Width\t=02{ending}{line}{tail}"
    blocks, trailing = nvram_parsing.find_blocks(text)
    updated = nvram_editor.NVRAMManager().update_value(blocks[0], new_value)
    assert rebuild_text([updated, blocks[1]], trailing) == f"Setup Question = Q{ending}Width\t=02{ending}{expected}{tail}"
    assert [f.value for f in updated.fields if isinstance(f, ValueField)] == [new_value]


def test_hexadecimal_settings_are_described_with_both_forms() -> None:
    blocks, _ = nvram_parsing.find_blocks("Setup Question = A\nValue =FF5\n\nSetup Question = B\nValue =<30>\n")
    assert [nvram_safety.current_setting(b) for b in blocks] == ["Value 0xFF5 (4085)", "Value <30>"]


def test_a_number_with_a_tail_blocks_the_import_check() -> None:
    before = "Setup Question = Q\nValue =3FF\n"
    assert nvram_safety.structural_changes(before, "Setup Question = Q\nValue =10FF\n") == []  # a real edit
    assert nvram_safety.structural_changes(before, "Setup Question = Q\nValue =10 FF\n")


def test_zero_padded_value_line_loads() -> None:
    blocks, _ = nvram_parsing.find_blocks("Setup Question = Fan\nValue =<08>\n")
    assert [f.value for f in blocks[0].fields if isinstance(f, ValueField)] == [8]


# --- Width bounds the value when there is no Min/Max ------------------------


@pytest.mark.parametrize(
    "name, width, largest",
    [
        ("SataRedrv Com Option1 Address", 1, 255),
        ("Adjust VID Value", 2, 65535),
        ("Current VDDIO Value (mV)", 4, 4294967295),
    ],
)
def test_width_sets_the_value_limits(manager, name: str, width: int, largest: int) -> None:
    block = live(manager, name)
    assert block.width == width
    assert block.value_bounds() == (0, largest)
    idx = index_of(manager, name)

    nvram_editor.apply_changes(manager, manager.blocks, [idx], None, largest)  # fits
    with pytest.raises(ValueError, match=f"{width}-byte"):
        nvram_editor.apply_changes(manager, manager.blocks, [idx], None, largest + 1)
    with pytest.raises(ValueError, match="negative"):
        nvram_editor.apply_changes(manager, manager.blocks, [idx], None, -1)


def test_explicit_min_max_override_the_width() -> None:
    blocks, _ = nvram_parsing.find_blocks("Setup Question = X\nWidth =01\nMin = 2\nMax = 5\nValue =<3>\n")
    assert blocks[0].value_bounds() == (2, 5)


def test_no_width_and_no_limits_means_unbounded() -> None:
    blocks, _ = nvram_parsing.find_blocks("Setup Question = X\nValue =<3>\n")
    assert blocks[0].value_bounds() == (None, None)


def test_min_max_inside_help_text_are_not_limits() -> None:
    text = "Setup Question = X\nHelp String = Range Min = 1 Max = 9 (see manual)\nWidth =01\nValue =<3>\n"
    blocks, _ = nvram_parsing.find_blocks(text)
    assert blocks[0].value_bounds() == (0, 255)
