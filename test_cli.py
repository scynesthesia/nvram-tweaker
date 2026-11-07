"""Tests for nvram_tweaker_cli.py: list / show / set, and export / import through SCEWIN."""
from __future__ import annotations

import functools
import json
import shutil
from pathlib import Path
from typing import List

import pytest

import nvram_cli_prompts
import nvram_editor
import nvram_tweaker_cli as cli
import scewin_runner
from nvram_version import __version__
from test_scewin_integration import SAMPLE_EXPORT, FakeRun

FIXTURE = Path(__file__).parent / "tests" / "fixtures" / "real_export_trimmed.txt"


@pytest.fixture
def export(tmp_path: Path) -> Path:
    target = tmp_path / "nvram.txt"
    shutil.copyfile(FIXTURE, target)
    return target


def run_cli(capsys, *argv: str, interactive: bool = False, answers: List[str] = ()):
    replies = iter(answers)
    code = cli.main([str(a) for a in argv], interactive=interactive, ask=lambda prompt: next(replies))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def changed_lines(before: bytes, after: bytes) -> List[tuple]:
    old, new = before.split(b"\n"), after.split(b"\n")
    assert len(old) == len(new)
    return [(a, b) for a, b in zip(old, new) if a != b]


# --- list / show -------------------------------------------------------------


def test_version_flag(capsys) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--version"])
    assert exit_info.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_list_shows_active_questions_with_flags_and_hides_inactive_ones(export, capsys) -> None:
    code, out, _ = run_cli(capsys, "list", export)
    assert code == 0
    assert out.count("PCIe/GFX Lanes Configuration") == 1  # the commented-out twin is not listed
    xhci = next(line for line in out.splitlines() if "XHCI Controller0 enable" in line)
    assert " 1C1 " in xhci and "!" in xhci and "[0F]Auto" in xhci
    aspm = next(line for line in out.splitlines() if "ASPM Mode Control" in line)
    assert "~" in aspm and "!" not in aspm
    assert "18 question(s)" in out


def test_list_json_is_machine_readable(export, capsys) -> None:
    code, out, _ = run_cli(capsys, "list", export, "Adjust VID", "--json")
    (row,) = json.loads(out)
    assert code == 0
    assert row == {
        "line": 75,
        "token": "18",
        "name": "Adjust VID Value",
        "kind": "value",
        "setting": "Value <0>",
        "width": 2,
        "risk": [],
        "note": [],
    }


def test_show_prints_range_and_marks_the_selected_option(export, capsys) -> None:
    _, out, _ = run_cli(capsys, "show", export, "Adjust VID")
    assert "Range:    0 to 65535, 2-byte setting" in out

    _, out, _ = run_cli(capsys, "show", export, "XHCI Controller0")
    assert "  * [0F]Auto" in out and "    [00]Disabled" in out
    assert "confirm-level setting (XHCI)" in out


def test_show_json(export, capsys) -> None:
    _, out, _ = run_cli(capsys, "show", export, "SataRedrv Com", "--json")
    (item,) = json.loads(out)
    assert item["range"] == [0, 255] and item["width_bytes"] == 1 and item["kind"] == "value"


def test_no_match_says_so_and_mentions_inactive_variants(tmp_path, capsys) -> None:
    only_commented = tmp_path / "nvram.txt"
    only_commented.write_text("// Setup Question\t= Ghost\n// Value\t=<1>\n\nSetup Question\t= Real\nValue\t=<2>\n")
    code, _, err = run_cli(capsys, "show", only_commented, "Ghost")
    assert code == 1 and "inactive (commented-out)" in err
    code, _, err = run_cli(capsys, "show", only_commented, "Nothing")
    assert code == 1 and "No question matches" in err


# --- tokens ------------------------------------------------------------------


@pytest.mark.parametrize("token", ["1C1", "1c1", "0x1C1", "0x1c1", "001C1", " 1C1 "])
def test_token_is_compared_as_a_hex_number(export, capsys, token: str) -> None:
    code, out, _ = run_cli(capsys, "list", export, "--token", token)
    assert code == 0 and "XHCI Controller0 enable" in out and "1 question(s)" in out


def test_invalid_token_is_reported(export, capsys) -> None:
    code, _, err = run_cli(capsys, "list", export, "--token", "zzz")
    assert code == 1 and "Invalid token 'zzz'" in err


# --- set: previews and confirmation ------------------------------------------------


def test_dry_run_prints_the_change_and_touches_nothing(export, capsys) -> None:
    before = export.read_bytes()
    code, out, _ = run_cli(capsys, "set", export, "Adjust VID", "300", "--dry-run")
    assert code == 0
    assert "Adjust VID Value (Token: 18): Value <0> -> Value <300>" in out
    assert "[DRY RUN] Nothing was written." in out
    assert export.read_bytes() == before
    assert sorted(p.name for p in export.parent.iterdir()) == ["nvram.txt"]  # no .bak / .original


def test_set_without_yes_refuses_when_not_interactive(export, capsys) -> None:
    before = export.read_bytes()
    code, _, err = run_cli(capsys, "set", export, "Adjust VID", "300")
    assert code == 1 and "pass --yes" in err
    assert export.read_bytes() == before


def test_set_with_yes_changes_exactly_one_line_and_keeps_the_original(export, capsys) -> None:
    before = export.read_bytes()
    code, out, _ = run_cli(capsys, "set", export, "Adjust VID", "300", "--yes")
    assert code == 0 and "Saved" in out
    assert changed_lines(before, export.read_bytes()) == [(b"Value\t=<0>\r", b"Value\t=<300>\r")]
    assert (export.parent / "nvram.txt.bak").read_bytes() == before
    assert (export.parent / "nvram.original.txt").read_bytes() == before


def test_the_original_survives_later_saves(export, capsys) -> None:
    before = export.read_bytes()
    run_cli(capsys, "set", export, "Adjust VID", "300", "--yes")
    run_cli(capsys, "set", export, "Adjust VID", "400", "--yes")
    assert (export.parent / "nvram.original.txt").read_bytes() == before  # not the 300 version
    assert b"<300>" in (export.parent / "nvram.txt.bak").read_bytes()  # .bak is only the previous save


def test_setting_the_current_value_is_a_no_op(export, capsys) -> None:
    code, out, _ = run_cli(capsys, "set", export, "Adjust VID", "0", "--yes")
    assert code == 0 and "Nothing to change" in out
    assert not (export.parent / "nvram.txt.bak").exists()


def test_a_bare_value_keeps_its_form(export, capsys) -> None:
    before = export.read_bytes()
    code, _, _ = run_cli(capsys, "set", export, "SataRedrv Com", "7", "--yes")
    assert code == 0
    assert changed_lines(before, export.read_bytes()) == [(b"Value\t=0\r", b"Value\t=7\r")]


# --- set: risky settings -----------------------------------------------------------


def test_risky_change_needs_allow_risky_when_scripted(export, capsys) -> None:
    before = export.read_bytes()
    code, _, err = run_cli(capsys, "set", export, "XHCI Controller0", "Disabled", "--yes")
    assert code == 1 and "--allow-risky" in err
    assert export.read_bytes() == before

    code, _, _ = run_cli(capsys, "set", export, "XHCI Controller0", "Disabled", "--yes", "--allow-risky")
    assert code == 0
    assert len(changed_lines(before, export.read_bytes())) == 2  # the '*' moved


def test_note_level_change_needs_no_extra_flag(export, capsys) -> None:
    code, _, _ = run_cli(capsys, "set", export, "ASPM Mode Control", "Auto", "--yes")
    assert code == 0


def test_interactive_risky_change_asks_for_yes_then_for_confirmation(export, capsys) -> None:
    before = export.read_bytes()
    code, _, _ = run_cli(
        capsys, "set", export, "XHCI Controller0", "Disabled", interactive=True, answers=["no"]
    )
    assert code == 1 and export.read_bytes() == before

    code, _, _ = run_cli(
        capsys, "set", export, "XHCI Controller0", "Disabled", interactive=True, answers=["YES", "y"]
    )
    assert code == 0 and export.read_bytes() != before


# --- set: matching and validation ---------------------------------------------------


def test_ambiguous_match_is_refused_when_scripted(export, capsys) -> None:
    before = export.read_bytes()
    code, _, err = run_cli(capsys, "set", export, "Value", "5", "--yes")
    assert code == 2 and "3 questions match 'Value'" in err
    assert "token 18" in err and "token 11" in err
    assert export.read_bytes() == before


def test_token_narrows_an_ambiguous_match(export, capsys) -> None:
    code, _, _ = run_cli(capsys, "set", export, "Value", "5", "--token", "0x18", "--yes")
    assert code == 0
    assert b"Value\t=<5>" in export.read_bytes()


def test_all_changes_every_match(export, capsys) -> None:
    code, out, _ = run_cli(capsys, "set", export, "Value", "5", "--all", "--yes")
    assert code == 0 and "3 setting(s) will change" in out


def test_interactive_choice_among_several_matches(export, capsys) -> None:
    code, out, _ = run_cli(capsys, "set", export, "Value", "5", interactive=True, answers=["2", "y"])
    assert code == 0 and "1 setting(s) will change" in out
    assert b"Value\t=<5>" in export.read_bytes()


def test_interactive_choice_can_be_cancelled(export, capsys) -> None:
    before = export.read_bytes()
    code, _, _ = run_cli(capsys, "set", export, "Value", "5", interactive=True, answers=["q"])
    assert code == 1 and export.read_bytes() == before


@pytest.mark.parametrize(
    "query, value, message",
    [
        ("SataRedrv Com", "256", "largest a 1-byte setting can hold"),
        ("SataRedrv Com", "-1", "cannot be negative"),
        ("SataRedrv Com", "Disabled", "needs a number"),
        ("SataRedrv Com", "0x1G", "needs a number"),
        ("ASPM Mode Control", "Sometimes", "not found"),
    ],
)
def test_bad_values_are_rejected_before_anything_is_written(export, capsys, query, value, message) -> None:
    before = export.read_bytes()
    code, _, err = run_cli(capsys, "set", export, query, value, "--yes")
    assert code == 1 and message in err
    assert export.read_bytes() == before


def test_explicit_mode_overrides_auto(export, capsys) -> None:
    before = export.read_bytes()
    code, _, err = run_cli(capsys, "set", export, "ASPM Mode Control", "5", "--mode", "value", "--yes")
    assert code == 1 and "does not contain a Value line" in err
    assert export.read_bytes() == before


# --- the old command keeps working --------------------------------------------------


def test_legacy_nvram_editor_command_forwards_to_set(export, capsys) -> None:
    code = nvram_editor.main([str(export), "Adjust VID", "12", "--mode", "value", "--dry-run"])
    assert code == 0
    assert "Value <0> -> Value <12>" in capsys.readouterr().out


# --- confirm_write -------------------------------------------------------------------


def _confirm(**kwargs) -> tuple[bool, List[str]]:
    said: List[str] = []
    answers = iter(kwargs.pop("answers", []))
    ok = nvram_cli_prompts.confirm_write(
        "Go?", ask=lambda prompt: next(answers), say=said.append, **kwargs
    )
    return ok, said


def test_confirm_write_matrix() -> None:
    assert _confirm(yes=True, interactive=False)[0] is True
    assert _confirm(yes=False, interactive=False)[0] is False
    assert _confirm(yes=False, interactive=True, answers=["y"])[0] is True
    assert _confirm(yes=False, interactive=True, answers=["n"])[0] is False
    risky = ["X -> Y [RISK: XHCI]"]
    assert _confirm(risky_lines=risky, yes=True, interactive=False)[0] is False  # --yes is not enough
    assert _confirm(risky_lines=risky, yes=True, allow_risky=True, interactive=False)[0] is True
    assert _confirm(risky_lines=risky, yes=True, interactive=True, answers=["YES"])[0] is True
    assert _confirm(risky_lines=risky, yes=True, interactive=True, answers=["yeah"])[0] is False
    ok, said = _confirm(risky_lines=risky, yes=True, allow_risky=True, interactive=False)
    assert any("RISK" in line for line in said)  # risky changes are always shown


# --- diagnose, and export -> set -> import through SCEWIN -----------------------------


def test_diagnose_command(tmp_path, capsys) -> None:
    log = tmp_path / "log-file.txt"
    log.write_text("LoadDeviceDriver returned false\n")
    code, out, _ = run_cli(capsys, "diagnose", log)
    assert code == 1 and "administrator" in out


@pytest.fixture
def fake_scewin(monkeypatch, tmp_path):
    scewin_dir = tmp_path / "SCEWIN"
    scewin_dir.mkdir()
    for name in scewin_runner.REQUIRED_FILES:
        (scewin_dir / name).write_bytes(b"")
    fake = FakeRun()
    monkeypatch.setattr(
        scewin_runner, "SCEWINRunner", functools.partial(scewin_runner.SCEWINRunner, run=fake, admin_check=lambda: True)
    )
    return scewin_dir, fake


def test_export_set_import_restore_through_the_cli(fake_scewin, tmp_path, capsys) -> None:
    scewin_dir, fake = fake_scewin
    script = tmp_path / "work" / "nvram.txt"

    code, out, _ = run_cli(capsys, "export", "--scewin", scewin_dir, "--file", script)
    assert code == 0 and "restore point" in out
    assert (script.parent / "nvram.original.txt").is_file()

    # XHCI is a confirm-level setting, so both the edit and the import need --allow-risky when scripted.
    code, _, _ = run_cli(capsys, "set", script, "XHCI Hand-off", "Disabled", "--yes", "--allow-risky")
    assert code == 0

    calls = len(fake.calls)
    code, _, err = run_cli(capsys, "import", "--scewin", scewin_dir, "--file", script, "--yes")
    assert code == 1 and "--allow-risky" in err
    # Reading the live BIOS (/o) is fine; nothing may have been written (/i).
    assert all("/i" not in call for call in fake.calls[calls:])

    code, out, _ = run_cli(capsys, "import", "--scewin", scewin_dir, "--file", script, "--yes", "--allow-risky")
    assert code == 0 and "XHCI Hand-off" in out
    assert fake.calls[-1][1:3] == ["/i", "/s"]

    code, _, _ = run_cli(capsys, "restore", "--scewin", scewin_dir, "--file", script, "--yes")
    assert code == 0
    assert fake.calls[-1][-1] == str((script.parent / "nvram.original.txt").resolve())


def test_import_without_a_scewin_folder_is_a_clear_error(tmp_path, capsys, monkeypatch) -> None:
    monkeypatch.setattr(scewin_runner, "default_scewin_candidates", lambda: [tmp_path])
    code, _, err = run_cli(capsys, "export", "--file", tmp_path / "nvram.txt")
    assert code == 1 and "Could not find a SCEWIN folder" in err


def test_a_filesystem_error_is_reported_not_raised(export, capsys, monkeypatch) -> None:
    def deny(*args, **kwargs):
        raise PermissionError("access is denied")

    monkeypatch.setattr(nvram_editor, "_write_atomically", deny)
    before = export.read_bytes()
    code, _, err = run_cli(capsys, "set", export, "Adjust VID", "300", "--yes")
    assert code == 1 and "access is denied" in err
    assert export.read_bytes() == before


def test_import_without_a_restore_point_reads_the_live_bios_first(fake_scewin, tmp_path, capsys) -> None:
    scewin_dir, fake = fake_scewin
    script = tmp_path / "nvram.txt"
    script.write_text(SAMPLE_EXPORT.replace("Value\t=<30>", "Value\t=<45>"), encoding="utf-8")  # no restore point yet

    code, out, _ = run_cli(capsys, "import", "--scewin", scewin_dir, "--file", script, "--yes")

    assert code == 0 and "Fan Start Temperature" in out and "nvram.preimport.txt" in out
    assert fake.calls[0][1] == "/o" and fake.calls[-1][1] == "/i"  # read first, then write
    assert (tmp_path / "nvram.original.txt").is_file()  # the snapshot became the restore point


def test_import_without_a_baseline_is_refused_with_no_snapshot(fake_scewin, tmp_path, capsys) -> None:
    scewin_dir, fake = fake_scewin
    script = tmp_path / "nvram.txt"
    script.write_text(SAMPLE_EXPORT, encoding="utf-8")

    code, _, err = run_cli(capsys, "import", "--scewin", scewin_dir, "--file", script, "--yes", "--no-snapshot")

    assert code == 1 and "nothing to compare" in err
    assert fake.calls == []


def test_import_with_an_edited_offset_is_refused_even_with_yes(fake_scewin, tmp_path, capsys) -> None:
    scewin_dir, fake = fake_scewin
    script = tmp_path / "nvram.txt"
    run_cli(capsys, "export", "--scewin", scewin_dir, "--file", script)
    script.write_text(script.read_text(encoding="utf-8").replace("Offset\t=12", "Offset\t=FF"), encoding="utf-8")

    code, _, err = run_cli(capsys, "import", "--scewin", scewin_dir, "--file", script, "--yes", "--allow-risky")

    assert code == 1 and "differ in more than the selected option" in err
    assert all(call[1] != "/i" for call in fake.calls)
