"""Turn SCEWIN/AMISCE console output into plain-language explanations.

SCEWIN prints terse messages such as ``ERROR:57`` or ``LoadDeviceDriver returned
false``. :func:`diagnose` scans the captured output (or an existing
``log-file.txt``) and returns one :class:`Diagnosis` per known problem, in the
order the problems first appear.

The explanations are written for this project. For a longer community-maintained
list of messages see https://github.com/ab3lkaizen/SCEHUB#solutions-for-various-error-messages
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Pattern, Tuple

REFERENCE_URL = "https://github.com/ab3lkaizen/SCEHUB#solutions-for-various-error-messages"


@dataclass(frozen=True)
class Diagnosis:
    """A known SCEWIN message with its likely cause and a suggested fix."""

    key: str
    severity: str  # "error", "warning" or "info"
    title: str
    cause: str
    fix: str

    def format(self) -> str:
        return f"[{self.severity.upper()}] {self.title}\n  Cause: {self.cause}\n  Fix: {self.fix}"


_KNOWN_MESSAGES: Tuple[Tuple[Pattern[str], Diagnosis], ...] = (
    (
        re.compile(r"LoadDeviceDriver returned false|Unable to load Driver", re.IGNORECASE),
        Diagnosis(
            key="driver",
            severity="error",
            title="SCEWIN could not load its kernel driver",
            cause="amifldrv64.sys / amigendrv64.sys are missing from the SCEWIN folder, "
            "or SCEWIN was not started as administrator.",
            fix="Keep both .sys files next to SCEWIN_64.exe and run the tool as administrator.",
        ),
    ),
    (
        re.compile(r"ERROR\s*:\s*57", re.IGNORECASE),
        Diagnosis(
            key="error57",
            severity="error",
            title="SCEWIN rejected the command line or could not open the script file",
            cause="An argument was wrong, or the NVRAM script path does not exist.",
            fix="Check that the script file exists at the path being imported/exported.",
        ),
    ),
    (
        re.compile(r"HII data does not have setup questions", re.IGNORECASE),
        Diagnosis(
            key="no_hii",
            severity="error",
            title="The firmware is not publishing its setup questions (HII data)",
            cause="Some boards hide HII resources from the OS by default.",
            fix="On ASUS boards enable 'Publish HII Resources' under Setup > Tool. "
            "Other vendors may have no workaround.",
        ),
    ),
    (
        re.compile(r"Retrieving HII Database|BIOS not compatible", re.IGNORECASE),
        Diagnosis(
            key="bios_incompatible",
            severity="error",
            title="SCEWIN reports the BIOS as not compatible",
            cause="The BIOS is not AMI-based, the SmiVariable module is missing/outdated, "
            "or this SCEWIN version is too old for the board.",
            fix="Confirm an AMI BIOS (`systeminfo | findstr /I /C:BIOS`) and try a newer SCEWIN.",
        ),
    ),
    (
        re.compile(r"Platform identification failed", re.IGNORECASE),
        Diagnosis(
            key="platform_id",
            severity="error",
            title="Platform identification failed",
            cause="The Aptio core is older than SCEWIN expects.",
            fix="SCEWIN's /d option overrides this check; only use it if you understand the risk.",
        ),
    ),
    (
        re.compile(r"Error in writing variable", re.IGNORECASE),
        Diagnosis(
            key="write_protected",
            severity="error",
            title="A variable could not be written to NVRAM",
            cause="The variable is write-protected, or a 'PCI Device' is disabled in Device Manager.",
            fix="Re-enable any disabled PCI Devices. On recent ASUS/ASRock boards disable "
            "'Password protection of Runtime Variables' in the BIOS.",
        ),
    ),
    (
        re.compile(r"System configuration not modified", re.IGNORECASE),
        Diagnosis(
            key="not_modified",
            severity="warning",
            title="Nothing was changed in NVRAM",
            cause="The imported file matched the current settings, or every write failed.",
            fix="If you expected changes, look for 'Error in writing variable' above.",
        ),
    ),
    (
        re.compile(r"Missing Current Setting", re.IGNORECASE),
        Diagnosis(
            key="missing_star",
            severity="warning",
            title="A question has no option marked with '*'",
            cause="An option block lost its current-selection marker.",
            fix="Mark exactly one option with '*' (or comment the question out) at the reported line.",
        ),
    ),
    (
        re.compile(r"Unmatched question", re.IGNORECASE),
        Diagnosis(
            key="unmatched_question",
            severity="warning",
            title="SCEWIN could not match a question to a current setting",
            cause="No option is marked with '*' for the reported token.",
            fix="Put '*' on the option the BIOS default suggests for that question.",
        ),
    ),
    (
        re.compile(r"Length of string for control", re.IGNORECASE),
        Diagnosis(
            key="string_length",
            severity="info",
            title="A string setting was left unchanged",
            cause="The string in the script is shorter than the firmware's minimum length "
            "(usually an empty default).",
            fix="Leave it alone; do not edit string values to silence this.",
        ),
    ),
)


def diagnose(output: str) -> List[Diagnosis]:
    """Return the known problems found in SCEWIN output, in order of first appearance."""

    found: List[Tuple[int, Diagnosis]] = []
    for pattern, diagnosis in _KNOWN_MESSAGES:
        match = pattern.search(output)
        if match:
            found.append((match.start(), diagnosis))
    found.sort(key=lambda item: item[0])
    return [diagnosis for _, diagnosis in found]


def has_errors(diagnoses: List[Diagnosis]) -> bool:
    return any(diagnosis.severity == "error" for diagnosis in diagnoses)


__all__ = ["Diagnosis", "REFERENCE_URL", "diagnose", "has_errors"]
