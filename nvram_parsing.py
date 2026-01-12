"""Parse AMI SCE NVRAM exports into blocks and fields.

The parser looks for ``Setup Question =`` headers, pulls out help strings,
tokens, numeric values, and options, and keeps the surrounding formatting so
the text can be rebuilt without losing layout details.
"""
from __future__ import annotations

import logging
import re
from typing import List, Optional, Tuple

from nvram_structures import BlockField, OptionField, QuestionBlock, ValueField

LOGGER = logging.getLogger(__name__)

BLOCK_PATTERN = re.compile(
    r"""
    (?P<header_line>^\s*(?:[#;/]{0,2}\s*)?Setup\s+Question\s*=\s*(?P<name>[^\r\n]*?))
    (?P<header_ending>\r?\n|\r|\n)
    (?P<body>
        (?:
            (?!
                ^\s*(?:[#;/]{0,2}\s*)?Setup\s+Question\b
            )
            [\s\S]
        )*?
    )
    (?=^\s*(?:[#;/]{0,2}\s*)?Setup\s+Question\b|\Z)
    """,
    re.IGNORECASE | re.MULTILINE | re.VERBOSE,
)
HELP_PATTERN = re.compile(
    r"^\s*(?:[#;/]{0,2}\s*)?Help\s+String\s*=\s*(?P<help>[^\r\n]*)",
    re.MULTILINE,
)
TOKEN_PATTERN = re.compile(
    r"^\s*(?:[#;/]{0,2}\s*)?Token\s*=\s*(?P<token>.+)",
    re.MULTILINE,
)
VALUE_PATTERN = re.compile(r"Value\s*=\s*<(?P<value>[+-]?(?:0x[0-9A-Fa-f]+|\d+))>\s*")
OPTION_LINE_PATTERN = re.compile(
    r"(?P<prefix>\s*(?:[#;/]{0,2}\s*)?(?:Options?|Option)?\s*=?\s*)?"
    r"(?P<star>\*)?\[(?P<code>[^\]]+)\](?P<label>.+)"
)
MIN_PATTERN = re.compile(r"\bMin\s*=\s*(?P<min>-?(?:0x[0-9A-Fa-f]+|\d+))", re.IGNORECASE)
MAX_PATTERN = re.compile(r"\bMax\s*=\s*(?P<max>-?(?:0x[0-9A-Fa-f]+|\d+))", re.IGNORECASE)
TOKEN_VALIDATION_PATTERN = re.compile(
    r"^(0x[0-9A-Fa-f]+|[0-9A-Fa-f]+)(\s*,\s*(0x[0-9A-Fa-f]+|[0-9A-Fa-f]+))*$"
)
NUMERIC_INPUT_PATTERN = re.compile(r"^[+-]?(?:0x[0-9A-Fa-f]+|\d+)$")
SETUP_BOUNDARY_PATTERN = re.compile(r"setup\s+question", re.IGNORECASE)


def find_blocks(text: str) -> tuple[List[QuestionBlock], str]:
    """Parse all blocks from raw text, preserving formatting metadata.

    Returns a tuple of ``(blocks, trailing_text)`` where ``trailing_text`` holds
    any content after the final parsed block to enable lossless reconstruction.
    """

    blocks: List[QuestionBlock] = []
    matches = list(BLOCK_PATTERN.finditer(text))
    if not matches:
        _warn_on_skipped_range(text, 0, len(text))
        trailing_text = text
        return blocks, trailing_text
    cursor = 0

    for match in matches:
        pre_separator = text[cursor:match.start()]
        _warn_on_skipped_range(text, cursor, match.start())
        line_number = text.count("\n", 0, match.start()) + 1
        header_line = match.group("header_line")
        leading_whitespace = _extract_leading_whitespace(header_line)
        header_without_leading = header_line[len(leading_whitespace) :]
        header_core, inline_comment = _split_inline_comment(header_without_leading)
        setup_match = re.match(
            r"(?P<prefix>(?:[#;/]{0,2}\s*)?Setup\s+Question\s*=\s*)(?P<name>.*)",
            header_core,
            re.IGNORECASE,
        )
        if not setup_match:
            continue
        body = match.group("body")
        help_match = HELP_PATTERN.search(body)
        help_text = help_match.group("help") if help_match else None
        raw_name = setup_match.group("name").strip()
        name = raw_name
        token_match = TOKEN_PATTERN.search(body)
        min_match = MIN_PATTERN.search(body)
        max_match = MAX_PATTERN.search(body)
        min_value = _parse_range_value(min_match, "min")
        max_value = _parse_range_value(max_match, "max")
        cursor = match.end()
        cleaned_token: Optional[str] = None
        if token_match:
            try:
                cleaned_token = _clean_token(token_match.group("token"))
            except ValueError as exc:
                LOGGER.warning("Token parsing skipped for '%s': %s", name, exc)
                cleaned_token = None
        fields = _parse_fields(body)
        blocks.append(
            QuestionBlock(
                name=name,
                body=body,
                help_string=help_text.strip() if help_text else None,
                token=cleaned_token,
                min_value=min_value,
                max_value=max_value,
                start=match.start(),
                end=match.end(),
                line_number=line_number,
                pre_separator=pre_separator,
                post_separator="",
                setup_line=header_core,
                line_ending=match.group("header_ending"),
                leading_whitespace=leading_whitespace,
                inline_comment=inline_comment,
                fields=fields,
            )
        )
    trailing_text = text[cursor:]
    _warn_on_skipped_range(text, cursor, len(text))
    return blocks, trailing_text


def _extract_leading_whitespace(line: str) -> str:
    leading_match = re.match(r"^(\s*)", line)
    if not leading_match:
        return ""
    return leading_match.group(1)


def _split_inline_comment(line: str) -> tuple[str, str]:
    comment_match = re.search(r"(\s*(?:[#;/]{1,2}.*))$", line)
    if not comment_match or comment_match.start(1) == 0:
        return line, ""
    start_index = comment_match.start(1)
    return line[:start_index], comment_match.group(1)


def _warn_on_skipped_range(text: str, start: int, end: int) -> None:
    if start >= end:
        return

    segment = text[start:end]
    if not SETUP_BOUNDARY_PATTERN.search(segment):
        return

    start_line = text.count("\n", 0, start) + 1
    end_line = text.count("\n", 0, end) + 1
    LOGGER.warning(
        "Skipped potential Setup Question block between lines %d and %d due to unparsable format.",
        start_line,
        end_line,
    )


def _parse_range_value(match: Optional[re.Match[str]], group_name: str) -> Optional[int]:
    if not match:
        return None

    raw_value = match.group(group_name)
    try:
        return int(raw_value, 0)
    except ValueError:
        return None


def _clean_token(raw_token: str) -> str:
    token_without_comments = re.sub(r"//.*", "", raw_token)
    cleaned = token_without_comments.strip()

    if not cleaned:
        raise ValueError("Token is empty after removing comments.")

    if not TOKEN_VALIDATION_PATTERN.fullmatch(cleaned):
        raise ValueError(
            f"Token '{cleaned}' is not a valid decimal or hexadecimal list separated by commas."
        )

    return cleaned


def _parse_fields(body: str) -> List[BlockField]:
    fields: List[BlockField] = []
    for line in body.splitlines(keepends=True):
        option_match = OPTION_LINE_PATTERN.match(line)
        if option_match:
            fields.append(
                OptionField(
                    prefix=option_match.group("prefix") or "",
                    code=option_match.group("code"),
                    label=option_match.group("label").rstrip("\r\n"),
                    selected=bool(option_match.group("star")),
                    line_ending="\r\n" if line.endswith("\r\n") else ("\n" if line.endswith("\n") else ""),
                )
            )
            continue

        value_match = VALUE_PATTERN.search(line)
        if value_match:
            fields.append(
                ValueField(
                    value=int(value_match.group("value"), 0),
                    line_ending="\r\n" if line.endswith("\r\n") else ("\n" if line.endswith("\n") else ""),
                )
            )
    return fields


def validate_numeric_input(raw_value: str) -> tuple[int, Optional[str]]:
    """Validate decimal/hex input and return its integer value.

    Rejects tabs, newlines, or any characters outside the decimal/hex pattern
    to prevent hidden or malformed input from being applied.
    """

    if any(ch in raw_value for ch in ("\n", "\r", "\t")):
        raise ValueError("Numeric values must not contain tabs or newlines.")

    cleaned = raw_value.strip()
    if not cleaned:
        raise ValueError("Value cannot be empty.")

    if not NUMERIC_INPUT_PATTERN.fullmatch(cleaned):
        raise ValueError("Invalid value format: use decimal digits or 0x-prefixed hexadecimal.")

    parsed_value = int(cleaned, 0)
    conversion_note: Optional[str] = None
    lower_cleaned = cleaned.lower()
    if lower_cleaned.startswith("0x") or lower_cleaned.startswith("+0x") or lower_cleaned.startswith("-0x"):
        conversion_note = f"Entered hex value {cleaned} converted to decimal {parsed_value}."

    return parsed_value, conversion_note


def matches_name(block: QuestionBlock, query: str, exact: bool) -> bool:
    if exact:
        return block.name == query
    return query.lower() in block.name.lower()


def selected_option(body: str) -> Optional[str]:
    for line in body.splitlines():
        opt_match = OPTION_LINE_PATTERN.match(line)
        if opt_match and opt_match.group("star"):
            code = opt_match.group("code")
            label = opt_match.group("label").rstrip()
            return f"*[{code}]{label}"
    return None
