"""Safety checks shared by the CLI, the GUI and the SCEWIN runner.

* :func:`risky_keywords` flags question names that touch settings which can
  leave a machine without input, display or boot (USB/XHCI, PCIe/ASPM, CSM,
  voltages, ...). Callers ask for an extra confirmation when it returns matches.
* :func:`diff_blocks` lists the settings that differ between two parsed files,
  e.g. the untouched restore point and the file about to be imported.
* :func:`preflight_issues` catches problems SCEWIN would choke on before the
  file is written to NVRAM.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

from nvram_parsing import find_blocks
from nvram_reconstruction import rebuild_text
from nvram_structures import OptionField, QuestionBlock, ValueField

# Settings that commonly cause lost input/display, failed boots or hardware stress
# when changed blindly. Matching is case-insensitive on word boundaries.
RISK_KEYWORDS: Tuple[str, ...] = (
    "XHCI",
    "USB",
    "ASPM",
    "PCIe",
    "PCI Express",
    "CSM",
    "Secure Boot",
    "Fast Boot",
    "TPM",
    "fTPM",
    "PTT",
    "iGPU",
    "IGD",
    "Primary Display",
    "Voltage",
    "VCore",
    "Power Limit",
    "PL1",
    "PL2",
    "IccMax",
    "Current Limit",
    "SATA",
    "NVMe",
    "VMD",
    "BIOS Lock",
    "Flash Protection",
    "Watchdog",
)

# SCEWIN appends a '// Move "*" to the desired Option' hint to option lines.
_INLINE_COMMENT = re.compile(r"\s//")

_RISK_PATTERNS = tuple(
    (keyword, re.compile(rf"(?<![A-Za-z0-9]){re.escape(keyword)}(?![A-Za-z])", re.IGNORECASE))
    for keyword in RISK_KEYWORDS
)


def risky_keywords(name: str) -> List[str]:
    """Return the risk keywords found in a question name (empty if none)."""

    return [keyword for keyword, pattern in _RISK_PATTERNS if pattern.search(name)]


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


def current_setting(block: QuestionBlock) -> Optional[str]:
    """Describe a block's current value, e.g. ``Value <3>`` or ``[01]Enabled``."""

    for field in block.fields:
        if isinstance(field, ValueField):
            return f"Value <{field.value}>"
    for field in block.fields:
        if isinstance(field, OptionField) and field.selected:
            label = _INLINE_COMMENT.split(field.label, maxsplit=1)[0].strip() or field.label.strip()
            return f"[{field.code}]{label}"
    return None


@dataclass(frozen=True)
class SettingChange:
    name: str
    token: Optional[str]
    before: Optional[str]
    after: Optional[str]
    risk_keywords: Tuple[str, ...]

    def format(self) -> str:
        token = self.token or "None"
        flag = f"  [RISK: {', '.join(self.risk_keywords)}]" if self.risk_keywords else ""
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
                )
            )
    return changes


def diff_texts(before_text: str, after_text: str) -> List[SettingChange]:
    before_blocks, _ = find_blocks(before_text)
    after_blocks, _ = find_blocks(after_text)
    return diff_blocks(before_blocks, after_blocks)


def preflight_issues(text: str) -> List[str]:
    """Return reasons the text should not be imported (empty list means OK)."""

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
    "RISK_KEYWORDS",
    "SettingChange",
    "current_setting",
    "diff_blocks",
    "diff_texts",
    "find_risky_blocks",
    "preflight_issues",
    "risky_keywords",
]
