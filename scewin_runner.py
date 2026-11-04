"""Run AMI SCEWIN to export, import and restore NVRAM scripts.

nvram-tweaker does not ship SCEWIN. Point it at a folder containing
``SCEWIN_64.exe``, ``amifldrv64.sys`` and ``amigendrv64.sys`` (for example the
folder produced by SCEHUB's ``DL_SCEWIN.exe``: https://github.com/ab3lkaizen/SCEHUB).

Workflow
--------
1. ``export``: ``SCEWIN_64.exe /o /s <script>``. The first successful export also
   saves an untouched copy next to the script (``nvram.original.txt``): the
   restore point.
2. Edit the script with the CLI editor or the GUI.
3. ``import``: preflight checks (parser round-trip, exactly one ``*`` per option
   question), then ``SCEWIN_64.exe /i /s <script>``.
4. ``restore``: import the restore point to put every setting back.

Every run writes SCEWIN's output to ``log-file.txt`` next to the script and is
interpreted by :mod:`scewin_diagnostics`.

SCEWIN only runs on Windows with an AMI BIOS and needs administrator rights.
"""
from __future__ import annotations

import argparse
import ctypes
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Sequence

from nvram_safety import SettingChange, diff_texts, preflight_issues
from scewin_diagnostics import REFERENCE_URL, Diagnosis, diagnose, has_errors

SCEWIN_EXE = "SCEWIN_64.exe"
REQUIRED_FILES = (SCEWIN_EXE, "amifldrv64.sys", "amigendrv64.sys")
DEFAULT_SCRIPT_NAME = "nvram.txt"
LOG_FILE_NAME = "log-file.txt"
DEFAULT_TIMEOUT_SECONDS = 300

RunFunction = Callable[..., "subprocess.CompletedProcess[str]"]


class SCEWINError(Exception):
    """Raised when SCEWIN cannot be run or a safety check blocks the run."""


@dataclass(frozen=True)
class SCEWINResult:
    action: str
    command: List[str]
    returncode: int
    output: str
    log_path: Path
    diagnoses: List[Diagnosis] = field(default_factory=list)
    restore_point_created: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not has_errors(self.diagnoses)

    def summary_lines(self) -> List[str]:
        status = "succeeded" if self.ok else f"failed (exit code {self.returncode})"
        lines = [f"SCEWIN {self.action} {status}. Output saved to {self.log_path}."]
        if self.restore_point_created:
            lines.append("Saved an untouched restore point of this export.")
        for diagnosis in self.diagnoses:
            lines.extend(diagnosis.format().splitlines())
        if self.diagnoses:
            lines.append(f"More help: {REFERENCE_URL}")
        return lines


def is_admin() -> bool:
    """Return True when the current process can load SCEWIN's driver."""

    if os.name == "nt":
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            return False
    geteuid = getattr(os, "geteuid", None)
    return bool(geteuid and geteuid() == 0)


def missing_files(directory: Path) -> List[str]:
    return [name for name in REQUIRED_FILES if not (directory / name).is_file()]


def find_scewin(directory: Path) -> Path:
    """Validate a SCEWIN folder and return it, or raise with what is missing."""

    directory = Path(directory).expanduser()
    if not directory.is_dir():
        raise SCEWINError(f"SCEWIN folder '{directory}' does not exist.")
    missing = missing_files(directory)
    if missing:
        raise SCEWINError(
            f"SCEWIN folder '{directory}' is missing: {', '.join(missing)}. "
            "Get SCEWIN with SCEHUB's DL_SCEWIN.exe (https://github.com/ab3lkaizen/SCEHUB)."
        )
    return directory


def guess_scewin_dir(candidates: Iterable[Path]) -> Optional[Path]:
    """Look for a SCEWIN folder in each candidate and in ``<candidate>/SCEWIN/<version>/``."""

    for candidate in candidates:
        candidate = Path(candidate)
        options = [candidate]
        nested = candidate / "SCEWIN"
        if nested.is_dir():
            options.extend(sorted((p for p in nested.iterdir() if p.is_dir()), reverse=True))
        for option in options:
            if option.is_dir() and not missing_files(option):
                return option
    return None


def default_scewin_candidates() -> List[Path]:
    app_dir = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
    return [Path.cwd(), app_dir]


def restore_point_path(script_path: Path) -> Path:
    return script_path.with_name(f"{script_path.stem}.original{script_path.suffix}")


class SCEWINRunner:
    def __init__(
        self,
        scewin_dir: Path,
        *,
        run: RunFunction = subprocess.run,
        admin_check: Callable[[], bool] = is_admin,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.scewin_dir = find_scewin(scewin_dir)
        self._run = run
        self._admin_check = admin_check
        self.timeout = timeout

    @property
    def exe_path(self) -> Path:
        return self.scewin_dir / SCEWIN_EXE

    def export(self, script_path: Path) -> SCEWINResult:
        script_path = Path(script_path).resolve()
        script_path.parent.mkdir(parents=True, exist_ok=True)
        result = self._execute("export", ["/o", "/s", str(script_path)], script_path)

        created = False
        restore_point = restore_point_path(script_path)
        if result.ok and script_path.is_file() and not restore_point.exists():
            shutil.copy2(script_path, restore_point)
            created = True
        if created:
            result = replace(result, restore_point_created=True)
        return result

    def check_import(self, script_path: Path) -> List[str]:
        """Return preflight problems for a script (empty list means it may be imported)."""

        script_path = Path(script_path)
        if not script_path.is_file():
            return [f"Script file '{script_path}' does not exist."]
        return preflight_issues(_read_text(script_path))

    def changes_since_export(self, script_path: Path) -> Optional[List[SettingChange]]:
        """Settings that differ from the restore point, or None if there is no restore point."""

        script_path = Path(script_path)
        restore_point = restore_point_path(script_path)
        if not restore_point.is_file() or not script_path.is_file():
            return None
        return diff_texts(_read_text(restore_point), _read_text(script_path))

    def import_script(self, script_path: Path) -> SCEWINResult:
        script_path = Path(script_path).resolve()
        issues = self.check_import(script_path)
        if issues:
            raise SCEWINError("Import blocked by preflight checks:\n- " + "\n- ".join(issues))
        return self._execute("import", ["/i", "/s", str(script_path)], script_path)

    def restore(self, script_path: Path) -> SCEWINResult:
        restore_point = restore_point_path(Path(script_path).resolve())
        if not restore_point.is_file():
            raise SCEWINError(
                f"No restore point found at '{restore_point}'. It is created by the first export."
            )
        return replace(self.import_script(restore_point), action="restore")

    def _execute(self, action: str, args: Sequence[str], script_path: Path) -> SCEWINResult:
        if not self._admin_check():
            raise SCEWINError("SCEWIN needs administrator rights. Re-run nvram-tweaker as administrator.")

        command = [str(self.exe_path), *args]
        try:
            completed = self._run(
                command,
                cwd=str(self.scewin_dir),
                capture_output=True,
                text=True,
                errors="replace",
                timeout=self.timeout,
            )
        except FileNotFoundError as exc:
            raise SCEWINError(f"Could not start {self.exe_path}: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise SCEWINError(f"SCEWIN did not finish within {self.timeout} seconds.") from exc
        except OSError as exc:
            raise SCEWINError(f"Could not run SCEWIN: {exc}") from exc

        output = (completed.stdout or "") + (completed.stderr or "")
        log_path = script_path.with_name(LOG_FILE_NAME)
        log_path.write_text(output, encoding="utf-8")
        return SCEWINResult(
            action=action,
            command=command,
            returncode=completed.returncode,
            output=output,
            log_path=log_path,
            diagnoses=diagnose(output),
        )


def _read_text(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        text = path.read_text(encoding="latin-1")
    return text.replace("\r\n", "\n")


def _confirm(prompt: str, expected: Sequence[str]) -> bool:
    return input(prompt).strip().lower() in expected


def _print_changes(changes: Optional[List[SettingChange]]) -> bool:
    """Print the diff against the restore point; return True if any change is risky."""

    if changes is None:
        print("No restore point found, so the changes cannot be listed. Export first to create one.")
        return False
    if not changes:
        print("No settings differ from the restore point.")
        return False
    print(f"{len(changes)} setting(s) differ from the restore point:")
    for change in changes:
        print(f"  - {change.format()}")
    return any(change.risk_keywords for change in changes)


def _parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export, import or restore NVRAM settings with SCEWIN.")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--scewin", type=Path, help="Folder with SCEWIN_64.exe and its drivers")
        p.add_argument("--file", type=Path, default=Path(DEFAULT_SCRIPT_NAME), help="NVRAM script path")

    add_common(sub.add_parser("export", help="Export current NVRAM settings to the script file"))
    import_parser = sub.add_parser("import", help="Write the script file to NVRAM")
    add_common(import_parser)
    import_parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompts")
    restore_parser = sub.add_parser("restore", help="Re-import the restore point saved by the first export")
    add_common(restore_parser)
    restore_parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")
    diagnose_parser = sub.add_parser("diagnose", help="Explain an existing SCEWIN log file")
    diagnose_parser.add_argument("log", type=Path, help="Path to log-file.txt")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)

    if args.command == "diagnose":
        try:
            diagnoses = diagnose(_read_text(args.log))
        except OSError as exc:
            print(f"error: {exc}")
            return 1
        if not diagnoses:
            print("No known SCEWIN messages found in this log.")
        for diagnosis in diagnoses:
            print(diagnosis.format())
        if diagnoses:
            print(f"More help: {REFERENCE_URL}")
        return 1 if has_errors(diagnoses) else 0

    try:
        scewin_dir = args.scewin or guess_scewin_dir(default_scewin_candidates())
        if scewin_dir is None:
            raise SCEWINError("Could not find a SCEWIN folder. Pass it with --scewin.")
        runner = SCEWINRunner(scewin_dir)

        if args.command == "export":
            result = runner.export(args.file)
        elif args.command == "import":
            issues = runner.check_import(args.file)
            if issues:
                raise SCEWINError("Import blocked by preflight checks:\n- " + "\n- ".join(issues))
            risky = _print_changes(runner.changes_since_export(args.file))
            if not args.yes:
                if risky and not _confirm(
                    "Some changes touch risky settings (marked RISK). Type YES to continue: ", ("yes",)
                ):
                    print("Import cancelled.")
                    return 1
                if not _confirm("Write these settings to NVRAM now? (Y/N): ", ("y", "yes")):
                    print("Import cancelled.")
                    return 1
            result = runner.import_script(args.file)
        else:
            if not args.yes and not _confirm(
                "Re-import the original export and undo all changes? (Y/N): ", ("y", "yes")
            ):
                print("Restore cancelled.")
                return 1
            result = runner.restore(args.file)
    except SCEWINError as exc:
        print(f"error: {exc}")
        return 1

    for line in result.summary_lines():
        print(line)
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
