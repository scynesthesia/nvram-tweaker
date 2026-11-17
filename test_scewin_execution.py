"""How SCEWIN is started and what happens when a write goes wrong.

Covers the audit fixes for the Windows exe (no stdin, no console window), the import
timeout, and the warning after a failed or interrupted write. SCEWIN is never executed.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import scewin_runner
from test_scewin_integration import FakeRun, make_runner, scewin_dir  # noqa: F401  (fixture)


def test_scewin_never_inherits_stdin(tmp_path: Path, scewin_dir: Path) -> None:
    run = FakeRun()
    make_runner(scewin_dir, run).export(tmp_path / "nvram.txt")
    assert run.kwargs[0]["stdin"] is subprocess.DEVNULL


def test_windows_runs_get_no_console_window_and_other_systems_none() -> None:
    assert scewin_runner._window_options(True) == {"creationflags": 0x08000000}
    assert scewin_runner._window_options(False) == {}


def test_import_gets_a_longer_timeout_than_reads(tmp_path: Path, scewin_dir: Path) -> None:
    run = FakeRun()
    runner = make_runner(scewin_dir, run)
    script = tmp_path / "nvram.txt"
    runner.export(script)
    runner.import_script(script)
    assert run.kwargs[0]["timeout"] == scewin_runner.DEFAULT_TIMEOUT_SECONDS
    assert run.kwargs[-1]["timeout"] == scewin_runner.DEFAULT_IMPORT_TIMEOUT_SECONDS
    assert scewin_runner.DEFAULT_IMPORT_TIMEOUT_SECONDS > scewin_runner.DEFAULT_TIMEOUT_SECONDS


def _hang(command, **kwargs):
    raise subprocess.TimeoutExpired(command, kwargs["timeout"])


def test_timeout_during_an_import_warns_about_a_partial_write(tmp_path: Path, scewin_dir: Path) -> None:
    script = tmp_path / "nvram.txt"
    make_runner(scewin_dir, FakeRun()).export(script)
    runner = scewin_runner.SCEWINRunner(scewin_dir, run=_hang, admin_check=lambda: True)
    with pytest.raises(scewin_runner.SCEWINError, match=r"while writing.*Do not reboot.*restore"):
        runner.import_script(script)


def test_timeout_while_reading_is_a_plain_error(tmp_path: Path, scewin_dir: Path) -> None:
    runner = scewin_runner.SCEWINRunner(scewin_dir, run=_hang, admin_check=lambda: True)
    with pytest.raises(scewin_runner.SCEWINError, match="did not finish") as info:
        runner.export(tmp_path / "nvram.txt")
    assert "Do not reboot" not in str(info.value)


def _import_result(tmp_path: Path, scewin_dir: Path, output: str, returncode: int = 0):
    script = tmp_path / "nvram.txt"
    make_runner(scewin_dir, FakeRun()).export(script)
    return make_runner(scewin_dir, FakeRun(output=output, returncode=returncode)).import_script(script)


def test_failed_write_warns_that_some_settings_may_be_written(tmp_path: Path, scewin_dir: Path) -> None:
    result = _import_result(tmp_path, scewin_dir, "Error in writing variable Setup\n")
    assert result.may_be_partial
    assert any("Do not reboot" in line for line in result.summary_lines())


def test_unexplained_failure_is_treated_as_possibly_partial(tmp_path: Path, scewin_dir: Path) -> None:
    assert _import_result(tmp_path, scewin_dir, "something odd\n", returncode=3).may_be_partial


def test_failure_before_any_write_does_not_cry_wolf(tmp_path: Path, scewin_dir: Path) -> None:
    result = _import_result(tmp_path, scewin_dir, "LoadDeviceDriver returned false\n", returncode=1)
    assert not result.ok and not result.may_be_partial
    assert not any("Do not reboot" in line for line in result.summary_lines())


def test_successful_import_and_failed_export_have_no_partial_warning(tmp_path: Path, scewin_dir: Path) -> None:
    assert not _import_result(tmp_path, scewin_dir, "System configuration modified\n").may_be_partial
    failed = make_runner(scewin_dir, FakeRun(output="boom", returncode=1)).export(tmp_path / "other.txt")
    assert not failed.may_be_partial


# --- a read of the BIOS must be fresh ---------------------------------------


class SilentRun:
    """SCEWIN that exits 0 with output this tool does not know, and writes ``written`` (or nothing)."""

    def __init__(self, written=None) -> None:
        self.written = written
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append(list(command))
        if "/o" in command and self.written is not None:
            Path(command[-1]).write_text(self.written, encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="some message nobody has seen before\n", stderr="")


@pytest.mark.parametrize("written", [None, "", "this is not an export\n", "// Setup Question = Only inactive\n"])
def test_a_stale_snapshot_never_passes_for_a_fresh_read(tmp_path: Path, scewin_dir: Path, written) -> None:
    script = tmp_path / "nvram.txt"
    runner = make_runner(scewin_dir, FakeRun())
    runner.export(script)
    stale = runner.snapshot(script)
    stale.write_text("yesterday's snapshot", encoding="utf-8")

    silent = scewin_runner.SCEWINRunner(scewin_dir, run=SilentRun(written), admin_check=lambda: True)
    with pytest.raises(scewin_runner.SCEWINError, match="did not produce a usable export"):
        silent.snapshot(script)
    assert stale.read_text(encoding="utf-8") == "yesterday's snapshot"  # put back, untouched
    assert not list(tmp_path.glob(".*.prev"))


@pytest.mark.parametrize("written", [None, "", "this is not an export\n"])
def test_a_silent_export_failure_keeps_the_old_file_and_makes_no_restore_point(
    tmp_path: Path, scewin_dir: Path, written
) -> None:
    script = tmp_path / "nvram.txt"
    script.write_text("my hand-edited script", encoding="utf-8")

    result = scewin_runner.SCEWINRunner(scewin_dir, run=SilentRun(written), admin_check=lambda: True).export(script)

    assert not result.ok and [d.key for d in result.diagnoses] == ["no_output"]
    assert any("did not produce a usable export" in line for line in result.summary_lines())
    assert script.read_text(encoding="utf-8") == "my hand-edited script"
    assert not (tmp_path / "nvram.original.txt").exists()  # an old file must never become the restore point
    assert not list(tmp_path.glob(".*.prev"))


def test_a_failed_read_puts_the_previous_file_back(tmp_path: Path, scewin_dir: Path) -> None:
    script = tmp_path / "nvram.txt"
    script.write_text("previous", encoding="utf-8")
    runner = scewin_runner.SCEWINRunner(scewin_dir, run=_hang, admin_check=lambda: True)
    with pytest.raises(scewin_runner.SCEWINError, match="did not finish"):
        runner.export(script)
    assert script.read_text(encoding="utf-8") == "previous"
    assert not list(tmp_path.glob(".*.prev"))


def test_a_log_that_cannot_be_saved_does_not_hide_the_result(tmp_path: Path, scewin_dir: Path) -> None:
    script = tmp_path / "nvram.txt"
    make_runner(scewin_dir, FakeRun()).export(script)
    (tmp_path / "log-file.txt").unlink()
    (tmp_path / "log-file.txt").mkdir()  # writing the log now fails

    result = make_runner(scewin_dir, FakeRun()).import_script(script)

    assert result.ok and result.log_error and "could not be saved" in result.log_error
    assert any("could not be saved" in line for line in result.summary_lines())
