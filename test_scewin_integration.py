"""Tests for the SCEWIN runner, log diagnostics and safety checks.

SCEWIN itself is never executed: a fake ``run`` stands in for ``subprocess.run``
and writes the export file the way SCEWIN would.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List

import pytest

import nvram_editor
import nvram_safety
import scewin_diagnostics
import scewin_runner

SAMPLE_EXPORT = """HIICrc32= 5B2C3F1A

// Script File Name : nvram.txt
// Created on 11/03/2026 at 10:00

Setup Question\t= Above 4G Decoding
Help String\t= Enables or Disables 64bit capable Devices to be Decoded in Above 4G Address Space
Token\t=5\t// Do NOT change this line
Offset\t=3D
Width\t=01
BIOS Default =[00]Disabled
Options\t=*[00]Disabled\t// Move "*" to the desired Option
         [01]Enabled

Setup Question\t= XHCI Hand-off
Help String\t= This is a workaround for OSes without XHCI hand-off support.
Token\t=1A\t// Do NOT change this line
Offset\t=12
Width\t=01
BIOS Default =[01]Enabled
Options\t=[00]Disabled\t// Move "*" to the desired Option
         *[01]Enabled

Setup Question\t= Fan Start Temperature
Help String\t= Temperature at which the fan starts.
Token\t=2B\t// Do NOT change this line
Offset\t=40
Width\t=01
BIOS Default =<30>
Value\t=<30>
"""


class FakeRun:
    def __init__(self, output: str = "System configuration modified\n", returncode: int = 0) -> None:
        self.calls: List[List[str]] = []
        self.kwargs: List[dict] = []
        self.output = output
        self.returncode = returncode

    def __call__(self, command, **kwargs):
        self.calls.append(list(command))
        self.kwargs.append(kwargs)
        if "/o" in command:
            Path(command[-1]).write_text(SAMPLE_EXPORT, encoding="utf-8")
        return subprocess.CompletedProcess(command, self.returncode, stdout=self.output, stderr="")


@pytest.fixture
def scewin_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "SCEWIN" / "5.05.01.0002"
    directory.mkdir(parents=True)
    for name in scewin_runner.REQUIRED_FILES:
        (directory / name).write_bytes(b"")
    return directory


def make_runner(scewin_dir: Path, run: FakeRun, admin: bool = True) -> scewin_runner.SCEWINRunner:
    return scewin_runner.SCEWINRunner(scewin_dir, run=run, admin_check=lambda: admin)


# --- diagnostics -----------------------------------------------------------


def test_diagnose_reports_known_messages_in_order() -> None:
    output = "ERROR:57 - Parsing CMD Line Arguments\nLoadDeviceDriver returned false\n"
    keys = [d.key for d in scewin_diagnostics.diagnose(output)]
    assert keys == ["error57", "driver"]


def test_diagnose_ignores_unknown_output() -> None:
    assert scewin_diagnostics.diagnose("Successfully exported\n") == []


def test_warnings_do_not_count_as_errors() -> None:
    diagnoses = scewin_diagnostics.diagnose("System configuration not modified")
    assert [d.key for d in diagnoses] == ["not_modified"]
    assert not scewin_diagnostics.has_errors(diagnoses)


# --- safety ----------------------------------------------------------------


@pytest.mark.parametrize(
    "name, expected",
    [
        ("XHCI Hand-off", ["XHCI"]),
        ("Legacy USB Support", ["USB"]),
        ("Native ASPM", ["ASPM"]),
        ("AMD fTPM switch", ["fTPM"]),
        ("Fan Start Temperature", []),
        ("Busy Wait", []),
    ],
)
def test_risky_keywords(name: str, expected: List[str]) -> None:
    assert nvram_safety.risky_keywords(name) == expected


def test_preflight_accepts_a_clean_export() -> None:
    assert nvram_safety.preflight_issues(SAMPLE_EXPORT) == []


def test_preflight_flags_missing_and_duplicate_selection() -> None:
    no_star = SAMPLE_EXPORT.replace("=*[00]Disabled", "=[00]Disabled", 1)
    two_stars = SAMPLE_EXPORT.replace("=[00]Disabled\t// Move", "=*[00]Disabled\t// Move", 1)
    assert any("no option marked" in issue for issue in nvram_safety.preflight_issues(no_star))
    assert any("2 options marked" in issue for issue in nvram_safety.preflight_issues(two_stars))


def test_preflight_rejects_non_export_text() -> None:
    assert nvram_safety.preflight_issues("hello world\n")


def test_diff_texts_flags_risky_changes() -> None:
    edited = SAMPLE_EXPORT.replace("=[00]Disabled\t// Move", "=*[00]Disabled\t// Move", 1).replace(
        "*[01]Enabled", "[01]Enabled", 1
    )
    changes = nvram_safety.diff_texts(SAMPLE_EXPORT, edited)
    assert len(changes) == 1
    change = changes[0]
    assert change.name == "XHCI Hand-off"
    assert change.before == "[01]Enabled"
    assert change.after.startswith("[00]Disabled")
    assert change.risk_keywords == ("XHCI",)


# --- runner ----------------------------------------------------------------


def test_find_scewin_lists_missing_files(tmp_path: Path) -> None:
    (tmp_path / "SCEWIN_64.exe").write_bytes(b"")
    with pytest.raises(scewin_runner.SCEWINError, match="amifldrv64.sys, amigendrv64.sys"):
        scewin_runner.find_scewin(tmp_path)


def test_guess_scewin_dir_finds_nested_version_folder(tmp_path: Path, scewin_dir: Path) -> None:
    assert scewin_runner.guess_scewin_dir([tmp_path]) == scewin_dir


def test_export_creates_restore_point_once(tmp_path: Path, scewin_dir: Path) -> None:
    run = FakeRun()
    runner = make_runner(scewin_dir, run)
    script = tmp_path / "nvram.txt"

    first = runner.export(script)
    assert first.ok and first.restore_point_created
    assert run.calls[0] == [str(scewin_dir / "SCEWIN_64.exe"), "/o", "/s", str(script.resolve())]
    assert run.kwargs[0]["cwd"] == str(scewin_dir)
    restore_point = tmp_path / "nvram.original.txt"
    assert restore_point.read_text(encoding="utf-8") == SAMPLE_EXPORT
    assert (tmp_path / "log-file.txt").read_text(encoding="utf-8") == run.output

    restore_point.write_text("keep me", encoding="utf-8")
    second = runner.export(script)
    assert not second.restore_point_created
    assert restore_point.read_text(encoding="utf-8") == "keep me"


def test_failed_export_does_not_create_restore_point(tmp_path: Path, scewin_dir: Path) -> None:
    run = FakeRun(output="LoadDeviceDriver returned false\n", returncode=1)
    result = make_runner(scewin_dir, run).export(tmp_path / "nvram.txt")
    assert not result.ok
    assert [d.key for d in result.diagnoses] == ["driver"]
    assert not (tmp_path / "nvram.original.txt").exists()


def test_runner_requires_admin(tmp_path: Path, scewin_dir: Path) -> None:
    run = FakeRun()
    with pytest.raises(scewin_runner.SCEWINError, match="administrator"):
        make_runner(scewin_dir, run, admin=False).export(tmp_path / "nvram.txt")
    assert run.calls == []


def test_import_is_blocked_by_preflight(tmp_path: Path, scewin_dir: Path) -> None:
    run = FakeRun()
    script = tmp_path / "nvram.txt"
    script.write_text(SAMPLE_EXPORT.replace("=*[00]Disabled", "=[00]Disabled", 1), encoding="utf-8")
    with pytest.raises(scewin_runner.SCEWINError, match="preflight"):
        make_runner(scewin_dir, run).import_script(script)
    assert run.calls == []


def test_import_runs_scewin_and_reports_errors(tmp_path: Path, scewin_dir: Path) -> None:
    run = FakeRun(output="Warning: Error in writing variable Setup to NVRAM\n")
    script = tmp_path / "nvram.txt"
    script.write_text(SAMPLE_EXPORT, encoding="utf-8")
    result = make_runner(scewin_dir, run).import_script(script)
    assert run.calls[0][1:] == ["/i", "/s", str(script.resolve())]
    assert not result.ok
    assert [d.key for d in result.diagnoses] == ["write_protected"]


def test_restore_imports_the_restore_point(tmp_path: Path, scewin_dir: Path) -> None:
    run = FakeRun()
    runner = make_runner(scewin_dir, run)
    script = tmp_path / "nvram.txt"
    runner.export(script)
    script.write_text(SAMPLE_EXPORT.replace("<30>", "<45>"), encoding="utf-8")

    changes = runner.changes_since_export(script)
    assert [(c.name, c.before, c.after) for c in changes] == [
        ("Fan Start Temperature", "Value <30>", "Value <45>")
    ]

    result = runner.restore(script)
    assert result.action == "restore"
    assert run.calls[-1][1:] == ["/i", "/s", str((tmp_path / "nvram.original.txt").resolve())]


def test_restore_without_restore_point_fails(tmp_path: Path, scewin_dir: Path) -> None:
    with pytest.raises(scewin_runner.SCEWINError, match="No restore point"):
        make_runner(scewin_dir, FakeRun()).restore(tmp_path / "nvram.txt")


def test_cli_diagnose_explains_a_log(tmp_path: Path, capsys) -> None:
    log = tmp_path / "log-file.txt"
    log.write_text("WARNING: HII data does not have setup questions information\n", encoding="utf-8")
    assert scewin_runner.main(["diagnose", str(log)]) == 1
    assert "Publish HII Resources" in capsys.readouterr().out


# --- editor CRC guard ------------------------------------------------------


def test_save_blocks_placeholder_crc_bypass_unless_forced(tmp_path: Path) -> None:
    path = tmp_path / "nvram.txt"
    path.write_text(SAMPLE_EXPORT, encoding="utf-8")
    manager = nvram_editor.NVRAMManager().load_file(path)
    new_text = manager.rebuild_text()

    with pytest.raises(ValueError, match="Unsafe CRC bypass blocked"):
        manager.save(path, new_text, bypass_crc=True, bypass_crc_mode="placeholder")

    manager.save(path, new_text, bypass_crc=True, bypass_crc_mode="placeholder", force_crc_bypass=True)
    assert path.read_text(encoding="utf-8").startswith("HIICrc32=<bypassed by nvram-tweaker")
