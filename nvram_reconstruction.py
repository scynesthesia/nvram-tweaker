"""Reconstruction helpers for converting parsed blocks back to text.

This module consumes :class:`QuestionBlock` instances produced by
``nvram_parsing.find_blocks`` and renders them back into the original format.
By preserving pre-separators, trailing content, inline comments, line endings,
and the raw ``body`` text, it enables round-trip editing without altering unrelated
content.
"""
from __future__ import annotations

from typing import List

from nvram_structures import QuestionBlock


def rebuild_text(blocks: List[QuestionBlock], trailing_text: str = "") -> str:
    """Render blocks into a contiguous text document.

    The ``trailing_text`` parameter preserves any bytes that appear after the
    final parsed block, avoiding duplication of separators between blocks.
    """

    parts = []
    for block in blocks:
        parts.append(block.pre_separator)
        parts.append(block.leading_whitespace)
        parts.append(block.setup_line)
        parts.append(block.inline_comment)
        parts.append(block.line_ending)
        parts.append(block.body)
    parts.append(trailing_text)
    return "".join(parts)


__all__ = ["rebuild_text"]
