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
        ("Setup Question = Second    ; spaced inline", "Second", "    ; spaced inline"),
        ("Setup Question = Third\t# hash inline", "Third", "\t# hash inline"),
        ("Setup Question = PCIe/GFX Lanes // note", "PCIe/GFX Lanes", " // note"),
    ],
)
def test_real_trailing_comments_are_still_split_off(header: str, name: str, comment: str) -> None:
    text = f"{header}\nValue =<1>\n"
    blocks, trailing = nvram_parsing.find_blocks(text)
    assert blocks[0].name == name and blocks[0].inline_comment == comment
    assert rebuild_text(blocks, trailing) == text


# --- commented-out questions ------------------------------------------------


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
