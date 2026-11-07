"""Keep the ``HIICrc32`` header of SCEWIN exports exactly as exported.

``HIICrc32`` is a checksum of the firmware's HII (setup) database, not of the
text in the script. SCEWIN uses it to recognise scripts that belong to the BIOS
it is running on, so editing settings never makes it stale and the tool never
rewrites it. Changing or removing it only defeats that check: a script exported
from another BIOS version could then be imported.

Real exports put the header on line 6, after a block of ``//`` comments, and end
some header lines with ``\\r\\r\\n``. Everything here works on the raw text and
touches nothing but the header line itself, so every other byte survives.

The helpers read the header, put it back if an edit dropped it, and report when
two scripts carry different headers (i.e. come from different BIOS builds).
"""
from __future__ import annotations

import re
from typing import Optional

CRC_HEADER_PREFIX = "HIICrc32"

# Where the first question starts; the header can only live in the text before it.
_FIRST_QUESTION = re.compile(r"(?m)^[ \t]*(?://[ \t]*)?Setup[ \t]+Question")
# The header line's content, without its line terminator (which may be \r\r\n).
_HEADER_LINE = re.compile(r"(?m)^[ \t]*" + CRC_HEADER_PREFIX + r"[^\r\n]*")


def _header_region_end(text: str) -> int:
    match = _FIRST_QUESTION.search(text)
    return match.start() if match else len(text)


def _find_header(text: str) -> Optional["re.Match[str]"]:
    return _HEADER_LINE.search(text, 0, _header_region_end(text))


def extract_crc_header(text: str) -> Optional[str]:
    """Return the ``HIICrc32`` line (without its line ending) if the header has one."""

    match = _find_header(text)
    return match.group(0) if match else None


def _insertion_offset(text: str) -> int:
    """Offset just after the last comment line of the leading block (0 if there is none)."""

    offset = 0
    after_last_comment = 0
    # Split on "\n" only: str.splitlines() would also cut at the lone "\r" inside "\r\r\n".
    for line in re.findall(r"[^\n]*\n|[^\n]+\Z", text[: _header_region_end(text)]):
        stripped = line.strip()
        if stripped.startswith(("//", "#", ";")):
            offset += len(line)
            after_last_comment = offset
        elif stripped == "":
            offset += len(line)
        else:
            break
    return after_last_comment


def _line_ending(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def preserve_crc_header(new_content: str, original_header: Optional[str]) -> str:
    """Return ``new_content`` carrying ``original_header`` verbatim.

    An existing ``HIICrc32`` line is replaced with the original one (only the
    line's content changes, never its line ending); if the line is missing it is
    inserted after the leading comments. Content without an original header is
    returned unchanged.
    """

    if not original_header:
        return new_content

    match = _find_header(new_content)
    if match:
        if match.group(0) == original_header:
            return new_content
        return new_content[: match.start()] + original_header + new_content[match.end() :]

    offset = _insertion_offset(new_content)
    eol = _line_ending(new_content)
    prefix = new_content[:offset]
    if prefix and not prefix.endswith(("\n", "\r")):
        prefix += eol
    return prefix + original_header + eol + new_content[offset:]


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
