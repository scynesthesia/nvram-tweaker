"""Keep the ``HIICrc32`` header of SCEWIN exports exactly as exported.

``HIICrc32`` is a checksum of the firmware's HII (setup) database, not of the
text in the script. SCEWIN uses it to recognise scripts that belong to the BIOS
it is running on, so editing settings never makes it stale and the tool never
rewrites it. Changing or removing it only defeats that check: a script exported
from another BIOS version could then be imported.

The helpers here read the header, put it back if an edit dropped it, and report
when two scripts carry different headers (i.e. come from different BIOS builds).
"""
from __future__ import annotations

from typing import List, Optional

CRC_HEADER_PREFIX = "HIICrc32"


def extract_crc_header(text: str) -> Optional[str]:
    """Return the ``HIICrc32`` line (without its line ending) if it is the first line."""

    first_line = text.splitlines()[0] if text else ""
    if first_line.lstrip().startswith(CRC_HEADER_PREFIX):
        return first_line
    return None


def _find_insertion_index(lines: List[str]) -> int:
    index = 0
    while index < len(lines):
        stripped = lines[index].lstrip()
        if stripped == "" or stripped.startswith(("#", "//", ";")):
            index += 1
            continue
        break
    return index


def preserve_crc_header(new_content: str, original_header: Optional[str]) -> str:
    """Return ``new_content`` carrying ``original_header`` verbatim.

    An existing ``HIICrc32`` line is replaced with the original one; if the line
    is missing it is inserted at the top (after leading comments). Content without
    an original header is returned unchanged.
    """

    if not original_header:
        return new_content

    lines = new_content.splitlines()
    had_trailing_newline = new_content.endswith(("\n", "\r"))
    for index, line in enumerate(lines):
        if line.lstrip().startswith(CRC_HEADER_PREFIX):
            if line == original_header:
                return new_content
            lines[index] = original_header
            break
    else:
        lines.insert(_find_insertion_index(lines), original_header)
        had_trailing_newline = had_trailing_newline or len(lines) == 1

    return "\n".join(lines) + ("\n" if had_trailing_newline else "")


def crc_header_mismatch(reference_text: str, candidate_text: str) -> Optional[str]:
    """Explain why two scripts look like they come from different BIOS builds, or None."""

    reference = extract_crc_header(reference_text)
    candidate = extract_crc_header(candidate_text)
    if reference is None or reference.strip() == (candidate or "").strip():
        return None
    if candidate is None:
        return f"The HIICrc32 header is missing; the original export has '{reference.strip()}'."
    return (
        f"The HIICrc32 header '{candidate.strip()}' differs from the original export's "
        f"'{reference.strip()}', so this script was probably exported from a different BIOS version."
    )


__all__ = ["CRC_HEADER_PREFIX", "crc_header_mismatch", "extract_crc_header", "preserve_crc_header"]
