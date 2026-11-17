"""User settings that live outside any NVRAM file. Today there is one: expert mode.

Expert mode removes the *interactive* extra confirmations for confirm-level settings: the
CLI's "type YES" and the GUI's risk dialogs and checkbox. It never touches the integrity
checks (one ``*`` per option, matching ``HIICrc32``, no missing or edited lines), the change
list, the snapshot taken before an import, or the final "write now?" question. Scripts are
not affected either: a non-interactive run still needs ``--allow-risky`` for risky settings,
so a setting forgotten in a config file can never change what an unattended job is allowed
to write.

It is off by default and can only be turned on together with an acknowledgement.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict

ENV_VAR = "NVRAM_TWEAKER_CONFIG"  # full path of the settings file (tests, portable installs)
SETTINGS_FILE_NAME = "settings.json"

ACKNOWLEDGEMENT = (
    "Expert mode stops asking before you change settings that can leave a board unbootable "
    "or without display or input (voltages, power limits, XHCI, CSM, Secure Boot, TPM, ...). "
    "I understand this can brick my machine and I know how to clear CMOS."
)


def settings_path() -> Path:
    """Where settings are stored: ``%APPDATA%\\nvram-tweaker`` on Windows, ``~/.config/nvram-tweaker`` elsewhere."""

    override = os.environ.get(ENV_VAR)
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "nvram-tweaker" / SETTINGS_FILE_NAME


def load() -> Dict[str, Any]:
    """The stored settings; a missing, unreadable or malformed file means defaults."""

    try:
        data = json.loads(settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(data: Dict[str, Any]) -> None:
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    try:
        temp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def expert_mode() -> bool:
    """True only when the stored value is exactly ``true`` (anything else means off)."""

    return load().get("expert_mode") is True


def set_expert_mode(enabled: bool, *, acknowledged: bool = False) -> None:
    """Turn expert mode on or off. Turning it on requires ``acknowledged=True``."""

    if enabled and not acknowledged:
        raise ValueError("Expert mode can only be turned on together with the acknowledgement.")
    data = load()
    data["expert_mode"] = bool(enabled)
    if enabled:
        data["expert_acknowledged_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    _save(data)
