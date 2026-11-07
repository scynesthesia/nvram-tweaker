"""Safety checks shared by the CLI, the GUI and the SCEWIN runner.

* :func:`risky_keywords` flags question names whose change can lock a board or
  harm hardware (XHCI, CSM, Secure Boot, TPM, voltages, power limits, ...).
  Callers ask for an explicit confirmation when it returns matches.
* :func:`noted_keywords` flags settings worth a second look (SATA, USB, PCIe,
  ASPM, ...) that are only marked in the change list, without a prompt. On a
  real export a quarter of all questions mention one of these words, so asking
  about every one would teach people to click through.
* :func:`diff_blocks` lists the settings that differ between two parsed files,
  e.g. the untouched restore point and the file about to be imported.
* :func:`missing_questions` finds questions that vanished from a file, which
  means it was truncated or damaged.
* :func:`preflight_issues` catches problems SCEWIN would choke on before the
  file is written to NVRAM.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

from nvram_parsing import OPTION_LINE_PATTERN, VALUE_PATTERN, find_blocks
from nvram_reconstruction import rebuild_text
from nvram_structures import OptionField, QuestionBlock, ValueField

# Changing these blindly can leave a machine without input, display or boot, wipe
# security state, or stress hardware. Matching is case-insensitive on word boundaries.
CONFIRM_KEYWORDS: Tuple[str, ...] = (
    "XHCI",
    "CSM",
    "Secure Boot",
    "TPM",
    "fTPM",
    "PTT",
    "iGPU",
    "IGD",
    "Primary Display",
    "Primary Video",
    "Voltage",
    "VCore",
    "Power Limit",
    "PL1",
    "PL2",
    "IccMax",
    "Current Limit",
    "BIOS Lock",
    "Flash Protection",
    "Erase",
)

# Worth noticing in the change list, but too common to prompt for.
NOTE_KEYWORDS: Tuple[str, ...] = (
    "USB",
    "ASPM",
    "PCIe",
    "PCI Express",
    "SATA",
    "NVMe",
    "VMD",
    "Fast Boot",
    "Watchdog",
)

# SCEWIN appends a '// Move "*" to the desired Option' hint to option lines.
_INLINE_COMMENT = re.compile(r"\s//")


def _compile(keywords: Iterable[str]) -> Tuple[Tuple[str, "re.Pattern[str]"], ...]:
    return tuple(
        (keyword, re.compile(rf"(?<![A-Za-z0-9]){re.escape(keyword)}(?![A-Za-z])", re.IGNORECASE))
        for keyword in keywords
    )


_CONFIRM_PATTERNS = _compile(CONFIRM_KEYWORDS)
_NOTE_PATTERNS = _compile(NOTE_KEYWORDS)


def risky_keywords(name: str) -> List[str]:
    """Return the confirm-level keywords found in a question name (empty if none)."""

    return [keyword for keyword, pattern in _CONFIRM_PATTERNS if pattern.search(name)]


def noted_keywords(name: str) -> List[str]:
    """Return the note-level keywords found in a question name (empty if none)."""

    return [keyword for keyword, pattern in _NOTE_PATTERNS if pattern.search(name)]


def find_risky_blocks(blocks: Iterable[QuestionBlock]) -> List[Tuple[QuestionBlock, List[str]]]:
    """Pair each block whose name matches a risk keyword with the matched keywords."""

    risky: List[Tuple[QuestionBlock, List[str]]] = []
    for block in blocks:
        if block.is_commented:
            continue
        keywords = risky_keywords(block.name)
        if keywords:
            risky.append((block, keywords))
    return risky


def option_text(field: OptionField) -> str:
    """An option as ``[01]Enabled``, without SCEWIN's trailing '// Move "*" ...' hint."""

    label = _INLINE_COMMENT.split(field.label, maxsplit=1)[0].strip() or field.label.strip()
    return f"[{field.code}]{label}"


def current_setting(block: QuestionBlock) -> Optional[str]:
    """Describe a block's current value, e.g. ``Value <3>`` or ``[01]Enabled``."""

    for field in block.fields:
        if isinstance(field, ValueField):
            return f"Value <{field.value}>"
    for field in block.fields:
        if isinstance(field, OptionField) and field.selected:
            return option_text(field)
    return None


@dataclass(frozen=True)
class SettingChange:
    name: str
    token: Optional[str]
    before: Optional[str]
    after: Optional[str]
    risk_keywords: Tuple[str, ...]
    note_keywords: Tuple[str, ...] = ()

    def format(self) -> str:
        token = self.token or "None"
        flag = f"  [RISK: {', '.join(self.risk_keywords)}]" if self.risk_keywords else ""
        if self.note_keywords:
            flag += f"  [note: {', '.join(self.note_keywords)}]"
        return f"{self.name} (Token: {token}): {self.before or '<none>'} -> {self.after or '<none>'}{flag}"


def _keyed(blocks: Iterable[QuestionBlock]) -> Dict[Tuple[str, Optional[str], int], QuestionBlock]:
    seen: Counter[Tuple[str, Optional[str]]] = Counter()
    keyed: Dict[Tuple[str, Optional[str], int], QuestionBlock] = {}
    for block in blocks:
        if block.is_commented:  # inactive variants are not settings
            continue
        base = (block.name, block.token)
        keyed[(block.name, block.token, seen[base])] = block
        seen[base] += 1
    return keyed


def diff_blocks(before: Iterable[QuestionBlock], after: Iterable[QuestionBlock]) -> List[SettingChange]:
    """List settings that differ between two parsed files.

    Blocks are matched by name, token and occurrence, so repeated questions with
    the same name are compared pairwise. Questions present on one side only are
    reported with ``None`` for the missing side.
    """

    before_map = _keyed(before)
    after_map = _keyed(after)
    changes: List[SettingChange] = []
    for key, after_block in after_map.items():
        before_block = before_map.get(key)
        old = current_setting(before_block) if before_block else None
        new = current_setting(after_block)
        if before_block is None or old != new:
            changes.append(
                SettingChange(
                    name=after_block.name,
                    token=after_block.token,
                    before=old,
                    after=new,
                    risk_keywords=tuple(risky_keywords(after_block.name)),
                    note_keywords=tuple(noted_keywords(after_block.name)),
                )
            )
    for key, before_block in before_map.items():
        if key not in after_map:
            changes.append(
                SettingChange(
                    name=before_block.name,
                    token=before_block.token,
                    before=current_setting(before_block),
                    after=None,
                    risk_keywords=tuple(risky_keywords(before_block.name)),
                    note_keywords=tuple(noted_keywords(before_block.name)),
                )
            )
    return changes


def diff_texts(before_text: str, after_text: str) -> List[SettingChange]:
    before_blocks, _ = find_blocks(before_text)
    after_blocks, _ = find_blocks(after_text)
    return diff_blocks(before_blocks, after_blocks)


def missing_questions(reference_text: str, candidate_text: str) -> List[str]:
    """Names of live questions in ``reference_text`` that ``candidate_text`` lost.

    A question that was commented out in the candidate counts as present: that is
    how a user deliberately skips a setting SCEWIN cannot write. One that is gone
    altogether means the file was truncated or damaged.
    """

    wanted: Counter[Tuple[str, Optional[str]]] = Counter()
    for block in find_blocks(reference_text)[0]:
        if not block.is_commented:
            wanted[(block.name, block.token)] += 1
    present: Counter[Tuple[str, Optional[str]]] = Counter()
    for block in find_blocks(candidate_text)[0]:
        present[(block.name, block.token)] += 1  # live or commented-out

    missing: List[str] = []
    for key, count in wanted.items():
        missing.extend([key[0]] * max(0, count - present[key]))
    return missing


def _mask_line(line: str) -> str:
    """A body line with the two things this tool may edit blanked out.

    The ``*`` marker of an option line (and SCEWIN's '// Move "*"' hint) and the number of
    a ``Value`` line are removed, so what is left must be identical in a file that was
    only edited with this tool.
    """

    text = line.rstrip()
    option = OPTION_LINE_PATTERN.match(text)
    if option:
        label = _INLINE_COMMENT.split(option.group("label"), maxsplit=1)[0].strip()
        return f"option [{option.group('code')}]{label}"
    value = VALUE_PATTERN.search(text)
    if value:
        start, end = value.span("value")
        return text[:start] + "#" + text[end:]
    return text


def structural_changes(reference_text: str, candidate_text: str) -> List[str]:
    """Describe live questions whose text differs in more than the selected option or number.

    Hand edits to ``Offset``, ``Width``, ``Token``, added or removed options, string values and
    so on do not show up in :func:`diff_blocks`, which only compares the current setting, yet
    SCEWIN would write them. Questions that are live in the candidate but were not live in the
    reference (new, or un-commented) are reported too.
    """

    reference = _keyed(find_blocks(reference_text)[0])
    problems: List[str] = []
    for key, block in _keyed(find_blocks(candidate_text)[0]).items():
        label = f"'{block.name}' (Token {block.token or 'none'})"
        before = reference.get(key)
        if before is None:
            problems.append(f"{label} was not an active question in the baseline")
            continue
        old = [_mask_line(line) for line in before.body.splitlines()]
        new = [_mask_line(line) for line in block.body.splitlines()]
        if old == new:
            continue
        sample = next((line for line in new if line not in old), None) or next(
            (line for line in old if line not in new), "(lines reordered)"
        )
        problems.append(f"{label}: {sample.strip()!r}")
    return problems


def preflight_issues(text: str) -> List[str]:
    """Return reasons the text should not be imported (empty list means OK).

    The parser round-trip below is a sanity check only: the parser keeps every
    byte it does not understand, so it holds for almost any text. The checks that
    can actually fail are the '*' counts here and :func:`missing_questions`.
    """

    issues: List[str] = []
    blocks, trailing = find_blocks(text)
    if not any(not block.is_commented for block in blocks):
        return ["No active 'Setup Question' blocks were found; this does not look like a SCEWIN export."]

    if rebuild_text(blocks, trailing_text=trailing) != text:
        issues.append("The file does not round-trip through the parser; refusing to import it.")

    for block in blocks:
        if block.is_commented:
            continue
        options = [field for field in block.fields if isinstance(field, OptionField)]
        if not options:
            continue
        selected = sum(1 for field in options if field.selected)
        if selected == 0:
            issues.append(f"'{block.name}' (line {block.line_number}) has no option marked with '*'.")
        elif selected > 1:
            issues.append(f"'{block.name}' (line {block.line_number}) has {selected} options marked with '*'.")
    return issues


__all__ = [
    "CONFIRM_KEYWORDS",
    "NOTE_KEYWORDS",
    "SettingChange",
    "current_setting",
    "diff_blocks",
    "diff_texts",
    "find_risky_blocks",
    "missing_questions",
    "noted_keywords",
    "option_text",
    "preflight_issues",
    "risky_keywords",
    "structural_changes",
]
