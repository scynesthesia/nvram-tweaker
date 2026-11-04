"""Small data objects for parsed NVRAM blocks and fields.

They keep the parsed values plus formatting hints so the editor can render the
file back without losing spacing or line endings.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import List, Union


@dataclass(frozen=True)
class ValueField:
    """Represents a ``Value = <X>`` line inside a block."""

    value: int
    line_ending: str = "\n"


@dataclass(frozen=True)
class OptionField:
    """Represents a selectable option line inside a block."""

    prefix: str
    code: str
    label: str
    selected: bool
    line_ending: str = "\n"

    def toggle(self, make_selected: bool) -> "OptionField":
        return replace(self, selected=make_selected)


BlockField = Union[OptionField, ValueField]


@dataclass(frozen=True)
class QuestionBlock:
    """Parsed representation of a Setup Question block with formatting info."""

    name: str
    body: str
    help_string: str | None
    token: str | None
    min_value: int | None
    max_value: int | None
    line_number: int
    pre_separator: str
    setup_line: str
    line_ending: str
    leading_whitespace: str
    inline_comment: str
    fields: List[BlockField]

    def with_body(self, new_body: str) -> "QuestionBlock":
        return replace(self, body=new_body)

    def with_fields(self, new_fields: List[BlockField]) -> "QuestionBlock":
        return replace(self, fields=new_fields)
