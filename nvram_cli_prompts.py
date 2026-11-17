"""Confirmation rules shared by every command that writes something.

Interactive runs ask; scripts must opt in explicitly, so a pipeline can never write
to a BIOS or a file by accident:

* ``--yes`` answers the plain "apply these changes?" question;
* ``--allow-risky`` is a separate flag for changes that can lock a board or harm
  hardware (see :mod:`nvram_safety`). ``--yes`` alone never covers them.
* Expert mode (:mod:`nvram_settings`) skips the typed YES for a person at a terminal;
  it does not apply to scripts and does not answer the final Y/N.
"""
from __future__ import annotations

import sys
from typing import Callable, Optional, Sequence

import nvram_settings


def is_interactive() -> bool:
    """True when a person can answer prompts (stdin is a terminal)."""

    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def _stderr(message: str) -> None:
    print(message, file=sys.stderr)


def confirm_write(
    what: str,
    *,
    risky_lines: Sequence[str] = (),
    yes: bool = False,
    allow_risky: bool = False,
    interactive: Optional[bool] = None,
    ask: Callable[[str], str] = input,
    say: Callable[[str], None] = _stderr,
) -> bool:
    """Decide whether to go ahead. Returns False (after explaining why) when not.

    ``risky_lines`` describes the confirm-level changes, if any.
    """

    if interactive is None:
        interactive = is_interactive()

    if risky_lines:
        say("These changes touch settings that can lock the board or harm hardware:")
        for line in risky_lines:
            say(f"  - {line}")
        # Expert mode only replaces the typed YES of a person at a terminal. A script still has
        # to pass --allow-risky, so a stored setting never widens what an unattended run may write.
        if interactive and not allow_risky and nvram_settings.expert_mode():
            say("Expert mode is on: not asking to confirm these.")
        elif not allow_risky:
            if not interactive:
                say("Refusing: pass --allow-risky to confirm risky changes when not running interactively.")
                return False
            if ask("Type YES to confirm the risky changes: ").strip().lower() != "yes":
                say("Cancelled.")
                return False

    if not yes:
        if not interactive:
            say("Refusing: pass --yes to confirm when not running interactively.")
            return False
        if ask(f"{what} (Y/N): ").strip().lower() not in ("y", "yes"):
            say("Cancelled.")
            return False
    return True
