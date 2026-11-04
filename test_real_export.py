"""Tests against a real SCEWIN export, not hand-written samples.

``tests/fixtures/real_export_trimmed.txt`` is a byte-exact excerpt (19 of 1,894
questions) of an export made by AMISCE 5.05.01.0002. It keeps the quirks of the
real format that invented samples missed:

* CRLF line endings, plus ``\\r\\r\\n`` after the Copyright and ``HIICrc32`` lines;
* the ``HIICrc32`` line is on line 6, after the comment block;
* a fully commented-out question next to a live question with the same name;
* numeric ``Value`` lines both as ``<N>`` and bare ``N``, at Width 01, 02 and 04.

Set ``NVRAM_REAL_EXPORT`` to the path of a full export to run the whole-file
checks too (skipped otherwise, including in CI).
"""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

import pytest

import nvram_crc
import nvram_editor
import nvram_parsing
import nvram_safety
import scewin_runner
from nvram_reconstruction import rebuild_text

FIXTURE = Path(__file__).parent / "tests" / "fixtures" / "real_export_trimmed.txt"
FULL_EXPORT = os.environ.get("NVRAM_REAL_EXPORT")


@pytest.fixture
def export_copy(tmp_path: Path) -> Path:
    target = tmp_path / "nvram.txt"
    shutil.copyfile(FIXTURE, target)
    return target


def fixture_text() -> str:
    return FIXTURE.read_bytes().decode("latin-1")


def test_fixture_keeps_the_real_line_endings() -> None:
    raw = FIXTURE.read_bytes()
    assert b"\r\r\n" in raw, "git rewrote the fixture's line endings; check .gitattributes"
    assert b"HIICrc32= F1F3871C\r\r\n" in raw


def test_crc_header_is_found_after_the_comment_block() -> None:
    text = fixture_text()
    assert text.splitlines()[0].startswith("//")
    assert nvram_crc.extract_crc_header(text) == "HIICrc32= F1F3871C"


def test_fixture_round_trips_and_passes_preflight() -> None:
    text = fixture_text()
    blocks, trailing = nvram_parsing.find_blocks(text)
    assert len(blocks) == 19
    assert rebuild_text(blocks, trailing) == text
    assert nvram_safety.preflight_issues(text) == []


def test_saving_without_edits_is_byte_identical(export_copy: Path) -> None:
    original = export_copy.read_bytes()
    manager = nvram_editor.NVRAMManager().load_file(export_copy)

    manager.save(export_copy, manager.rebuild_text())

    assert export_copy.read_bytes() == original
    assert (export_copy.parent / "nvram.txt.bak").read_bytes() == original


def test_an_option_edit_changes_only_the_two_star_lines(export_copy: Path) -> None:
    original = export_copy.read_bytes()
    manager = nvram_editor.NVRAMManager().load_file(export_copy)
    index = next(i for i, b in enumerate(manager.blocks) if b.name == "ASPM Mode Control")
    blocks = list(manager.blocks)
    current = nvram_safety.current_setting(blocks[index])
    other = next(
        f"{f.code}" for f in blocks[index].fields if f.__class__.__name__ == "OptionField" and not f.selected
    )
    blocks[index] = manager.update_options(blocks[index], f"[{other}]")
    manager.save(export_copy, manager.rebuild_text(blocks))

    before = original.split(b"\n")
    after = export_copy.read_bytes().split(b"\n")
    assert len(before) == len(after)
    assert sum(1 for a, b in zip(before, after) if a != b) == 2
    assert nvram_safety.current_setting(nvram_editor.NVRAMManager().load_file(export_copy).blocks[index]) != current


def test_import_is_blocked_when_the_header_differs_from_the_restore_point(tmp_path: Path) -> None:
    scewin_dir = tmp_path / "SCEWIN"
    scewin_dir.mkdir()
    for name in scewin_runner.REQUIRED_FILES:
        (scewin_dir / name).write_bytes(b"")
    script = tmp_path / "nvram.txt"
    shutil.copyfile(FIXTURE, tmp_path / "nvram.original.txt")
    script.write_bytes(FIXTURE.read_bytes().replace(b"F1F3871C", b"00000000"))

    runner = scewin_runner.SCEWINRunner(scewin_dir, admin_check=lambda: True)
    issues = runner.check_import(script)

    assert any("different BIOS version" in issue for issue in issues)


def test_a_missing_header_is_put_back_after_the_comments(export_copy: Path) -> None:
    manager = nvram_editor.NVRAMManager().load_file(export_copy)
    without_header = manager.rebuild_text().replace("HIICrc32= F1F3871C\r\r\n", "")
    assert "HIICrc32" not in without_header

    restored = nvram_crc.preserve_crc_header(without_header, manager.initial_crc)

    assert nvram_crc.extract_crc_header(restored) == "HIICrc32= F1F3871C"
    # Everything else is untouched, and the header sits after the comments, before the first question.
    assert restored.replace("HIICrc32= F1F3871C\r\n", "", 1) == without_header
    assert restored.index("HIICrc32") < restored.index("Setup Question")
    assert restored.index("All rights reserved.") < restored.index("HIICrc32")


@pytest.mark.skipif(not FULL_EXPORT, reason="set NVRAM_REAL_EXPORT to a full SCEWIN export to run")
def test_full_export_round_trips_and_saves_byte_identically(tmp_path: Path) -> None:
    source = Path(FULL_EXPORT)
    copy = tmp_path / "nvram.txt"
    shutil.copyfile(source, copy)  # never touch the original
    original = copy.read_bytes()

    manager = nvram_editor.NVRAMManager().load_file(copy)
    assert len(manager.blocks) > 100
    assert nvram_safety.preflight_issues(original.decode("latin-1")) == []

    # Every parsed name must equal the raw text of its header line (no truncation).
    raw_names = re.findall(
        r"(?m)^[ \t]*(?://[ \t]*)?Setup Question[ \t]*=[ \t]*(.*?)\r?$", original.decode("latin-1")
    )
    assert [b.name for b in manager.blocks] == [name.strip() for name in raw_names]

    manager.save(copy, manager.rebuild_text())

    assert copy.read_bytes() == original
