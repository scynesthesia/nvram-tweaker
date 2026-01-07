"""CRC preservation and bypass helpers for NVRAM files.

CRC semantics
-------------

Many AMI exports start with a ``HIICrc32=<value>`` line. Firmware tools often
expect this line to be preserved verbatim, even if it does not match the content
post-edit. The helpers here focus on restoring the originally loaded CRC value
or deliberately bypassing it (empty, remove, or placeholder modes). CRC values
are not recomputed; the module only reshapes existing headers.
"""
from __future__ import annotations

from typing import List, Optional

CRC_LINE_PATTERN = r"^HIICrc32=(?P<value>[^,\s]*)(?P<suffix>.*)$"


def _join_lines(lines: List[str], had_trailing_newline: bool) -> str:
    result = "\n".join(lines)
    return result + ("\n" if had_trailing_newline else "")


def _find_crc_insertion_index(lines: List[str]) -> int:
    insertion_index = 0
    while insertion_index < len(lines):
        stripped = lines[insertion_index].lstrip()
        if stripped.startswith(("#", "//", ";")) or stripped == "":
            insertion_index += 1
            continue
        break
    return insertion_index


def _extract_crc_suffix(crc_line: Optional[str]) -> str:
    import re

    if not crc_line:
        return ""
    match = re.match(CRC_LINE_PATTERN, crc_line)
    if not match:
        return ""
    return match.group("suffix")


def _contains_crc_marker(text: str) -> bool:
    import re

    return bool(re.search(r"^\s*HIICrc32\s*=", text, re.MULTILINE))


def recalculate_crc(
    new_content: str,
    initial_crc: Optional[str],
    *,
    bypass: bool = False,
    bypass_mode: str = "empty",
    tool_version: str = "0.0.0",
) -> str:
    """Return content with CRC header preserved or bypassed.

    When ``bypass`` is False, the original CRC header is restored or inserted at
    the top of the file (after leading comments). When ``bypass`` is True, the
    behavior depends on ``bypass_mode``:

    ``empty``
        Leave the line present but clear its value.
    ``remove``
        Remove the CRC line entirely.
    ``placeholder``
        Insert a versioned placeholder value, preserving any suffix
        (e.g., firmware version tags).
    """

    if bypass:
        return _apply_crc_bypass(new_content, initial_crc, bypass_mode, tool_version)

    if not initial_crc:
        return new_content

    lines = new_content.splitlines()
    had_trailing_newline = new_content.endswith(("\n", "\r"))

    for idx, line in enumerate(lines):
        if line.startswith("HIICrc32="):
            lines[idx] = initial_crc
            return _join_lines(lines, had_trailing_newline)

    insertion_index = _find_crc_insertion_index(lines)
    lines.insert(insertion_index, initial_crc)
    return _join_lines(lines, had_trailing_newline or not lines)


def _apply_crc_bypass(content: str, initial_crc: Optional[str], mode: str, tool_version: str) -> str:
    normalized_mode = mode.lower()
    if normalized_mode not in {"empty", "remove", "placeholder"}:
        raise ValueError(f"Unsupported CRC bypass mode '{mode}'.")

    lines = content.splitlines()
    had_trailing_newline = content.endswith(("\n", "\r"))
    placeholder_value = f"<bypassed by nvram-tweaker {tool_version}>"

    for idx, line in enumerate(lines):
        if not line.startswith("HIICrc32="):
            continue

        if normalized_mode == "remove":
            del lines[idx]
            return _join_lines(lines, had_trailing_newline)

        if normalized_mode == "placeholder":
            suffix = _extract_crc_suffix(line) or _extract_crc_suffix(initial_crc)
            lines[idx] = f"HIICrc32={placeholder_value}{suffix}"
            return _join_lines(lines, had_trailing_newline)

        lines[idx] = "HIICrc32="
        return _join_lines(lines, had_trailing_newline)

    if normalized_mode == "placeholder":
        suffix = _extract_crc_suffix(initial_crc)
        insertion_index = _find_crc_insertion_index(lines)
        lines.insert(insertion_index, f"HIICrc32={placeholder_value}{suffix}")
        return _join_lines(lines, had_trailing_newline or not lines)

    return content


__all__ = [
    "CRC_LINE_PATTERN",
    "_contains_crc_marker",
    "_extract_crc_suffix",
    "_find_crc_insertion_index",
    "_join_lines",
    "recalculate_crc",
]
