"""Tools for updating AMI SCE NVRAM text exports.

Finds ``Setup Question`` blocks and lets you move the selected option or update
numeric ``Value = <X>`` entries. Searches can be exact or partial, filtered by
token, and applied to one or many matching blocks.
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

from nvram_crc import extract_crc_header, preserve_crc_header
from nvram_parsing import (
    OPTION_LINE_PATTERN,
    VALUE_PATTERN,
    find_blocks,
    matches_name,
    normalize_token,
    selected_option,
)
from nvram_reconstruction import rebuild_text
from nvram_structures import OptionField, QuestionBlock, ValueField

LOGGER = logging.getLogger(__name__)


def _decode_bytes(raw_bytes: bytes, path: Path) -> tuple[str, str]:
    """Decode as UTF-8, falling back to latin-1 (which accepts every byte sequence)."""

    try:
        return raw_bytes.decode("utf-8"), "utf-8"
    except UnicodeDecodeError as exc:
        preview = raw_bytes[: exc.start].decode("utf-8", errors="ignore")
        line_number = preview.count("\n") + 1
        # Expected, not a problem: SCEWIN writes in a Windows code page (e.g. "85\xb0C" options).
        # latin-1 maps every byte to a character, so reading and saving is lossless.
        LOGGER.info("Read '%s' as latin-1 (non-UTF-8 byte near line %d).", path, line_number)
        return raw_bytes.decode("latin-1"), "latin-1"


def restore_point_path(script_path: Path) -> Path:
    """Where the untouched copy of an export lives: ``nvram.txt`` -> ``nvram.original.txt``."""

    return script_path.with_name(f"{script_path.stem}.original{script_path.suffix}")


def _write_atomically(path: Path, data: bytes) -> None:
    """Replace ``path`` with ``data`` so a crash can never leave a half-written file."""

    temp_path = path.with_name(f".{path.name}.tmp")
    try:
        with temp_path.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            shutil.copymode(path, temp_path)
        except OSError:
            pass  # keep default permissions if the original's cannot be read
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


class NVRAMManager:
    def __init__(self) -> None:
        self.path: Optional[Path] = None
        self.blocks: List[QuestionBlock] = []
        self.trailing_text: str = ""
        self.initial_crc: Optional[str] = None
        self._encoding: str = "utf-8"
        self.last_saved_text: Optional[str] = None

    def load_file(self, path: Path) -> "NVRAMManager":
        if not path.exists():
            raise ValueError(f"The file '{path}' does not exist. Please provide a valid path.")

        try:
            raw_bytes = path.read_bytes()
        except OSError as exc:
            raise ValueError(f"Unable to read '{path}': {exc}") from exc

        text, self._encoding = _decode_bytes(raw_bytes, path)

        try:
            self.blocks, self.trailing_text = find_blocks(text)
        except re.error as exc:
            raise ValueError(f"Unable to parse Setup Question blocks: {exc}") from exc

        self.path = path
        self.last_saved_text = text
        self.initial_crc = extract_crc_header(text)
        return self

    def update_value(self, block: QuestionBlock, new_value: int) -> QuestionBlock:
        _require_editable(block)
        body = block.body
        value_match = VALUE_PATTERN.search(body)
        if not value_match:
            raise ValueError("Block does not contain a Value line")
        # Replace only the number, so the form (``<30>`` or bare ``30``), the spacing
        # and the line ending all survive.
        start, end = value_match.span("value")
        new_body = body[:start] + str(new_value) + body[end:]
        new_fields: List[OptionField | ValueField] = []
        for field in block.fields:
            if isinstance(field, ValueField):
                new_fields.append(replace(field, value=new_value))
            else:
                new_fields.append(field)
        return block.with_body(new_body).with_fields(new_fields)

    def update_options(self, block: QuestionBlock, option_label: str) -> QuestionBlock:
        _require_editable(block)

        def _normalize(text: str) -> str:
            return re.sub(r"\s+", "", text).lower()

        normalized_target = _normalize(option_label)
        lines = block.body.splitlines(keepends=True)

        option_labels: List[str] = []
        for line in lines:
            opt_match = OPTION_LINE_PATTERN.match(line)
            if not opt_match:
                continue
            code = opt_match.group("code")
            label = opt_match.group("label").rstrip("\r\n")
            option_labels.append(_normalize(f"[{code}]{label}"))

        exact_matches = [idx for idx, label in enumerate(option_labels) if label == normalized_target]
        substring_matches = [idx for idx, label in enumerate(option_labels) if normalized_target in label]

        target_indices: List[int]
        if len(exact_matches) == 1:
            target_indices = exact_matches
        elif len(exact_matches) > 1:
            raise ValueError(f"Ambiguous option '{option_label}': matches multiple choices")
        elif len(substring_matches) == 1:
            target_indices = substring_matches
        elif len(substring_matches) > 1:
            raise ValueError(f"Ambiguous option '{option_label}': matches multiple choices")
        else:
            raise ValueError(f"Option '{option_label}' not found in block")

        target_option_index = target_indices[0]

        new_lines: List[str] = []
        new_fields: List[OptionField | ValueField] = []
        option_counter = 0

        for line in lines:
            opt_match = OPTION_LINE_PATTERN.match(line)
            if not opt_match:
                new_lines.append(line)
                continue

            prefix = opt_match.group("prefix")
            code = opt_match.group("code")
            label = opt_match.group("label").rstrip("\r\n")
            # Keep the line's real terminator, including odd ones such as "\r\r\n".
            line_ending = line[len(line.rstrip("\r\n")) :]

            is_target = option_counter == target_option_index
            if is_target:
                new_lines.append(f"{prefix}*[{code}]{label}{line_ending}")
            else:
                new_lines.append(f"{prefix}[{code}]{label}{line_ending}")

            option_counter += 1

        option_counter = 0
        for field in block.fields:
            if isinstance(field, OptionField):
                new_fields.append(field.toggle(option_counter == target_option_index))
                option_counter += 1
            else:
                new_fields.append(field)

        return block.with_body("".join(new_lines)).with_fields(new_fields)

    def matching_indices(self, query: str, exact: bool, token: Optional[str]) -> List[int]:
        """Positions of the active (not commented-out) questions that match.

        ``token`` is compared as a hexadecimal number, so ``7D1``, ``0x7d1`` and
        ``007D1`` all select the same question.
        """

        wanted: Optional[str] = None
        if token is not None:
            try:
                wanted = normalize_token(token)
            except ValueError:
                raise ValueError(
                    f"Invalid token '{token}': tokens are hexadecimal as printed in the export, e.g. 7D1 or 0x7D1."
                ) from None

        indices = []
        for index, block in enumerate(self.blocks):
            if block.is_commented or not matches_name(block, query, exact):
                continue
            if wanted is not None and (block.token is None or normalize_token(block.token) != wanted):
                continue
            indices.append(index)
        return indices

    def filter_blocks(self, query: str, exact: bool, token: Optional[str]) -> List[QuestionBlock]:
        return [self.blocks[i] for i in self.matching_indices(query, exact, token)]

    def save(self, path: Path, new_content: str) -> None:
        """Write ``new_content`` to ``path`` after backing up the current file.

        The original ``HIICrc32`` header is always written back verbatim; see
        :mod:`nvram_crc` for why it is never modified.
        """

        if self.path is None:
            raise ValueError("No file is currently loaded; cannot save changes.")

        self._verify_reparse_consistency(new_content)

        try:
            current_disk_text = self._read_text_from_disk(path)
        except ValueError as exc:
            raise ValueError(f"Unable to read the current file for backup: {exc}") from exc

        if self.last_saved_text is not None and current_disk_text != self.last_saved_text:
            raise ValueError(
                "The on-disk file has changed since it was loaded. Reload and re-apply your changes to avoid "
                "overwriting external edits."
            )

        # The first save also keeps the untouched file, so the original survives any number
        # of later saves (the .bak only ever holds the previous version).
        restore_point = restore_point_path(path)
        if not path.stem.endswith(".original") and not restore_point.exists():
            shutil.copyfile(path, restore_point)
        # A byte-for-byte copy: re-encoding or newline translation would alter the backup.
        shutil.copyfile(path, path.with_name(path.name + ".bak"))

        # Edits keep each line's own ending, so no newline conversion is done here:
        # real exports mix \r\n and \r\r\n and must be written back exactly.
        updated_content = preserve_crc_header(new_content, self.initial_crc)
        _write_atomically(path, updated_content.encode(self._encoding))
        self.last_saved_text = updated_content
        self._refresh_state_from_text(updated_content)

    def rebuild_text(self, blocks: Optional[List[QuestionBlock]] = None) -> str:
        """Round-trip blocks back to text while preserving formatting.

        Accepts an explicit list of blocks to render; defaults to the manager's
        current blocks. This thin wrapper allows callers (including the GUI
        layer) to access the reconstruction routine without re-importing it.
        """

        render_blocks = blocks if blocks is not None else self.blocks
        return rebuild_text(render_blocks, trailing_text=self.trailing_text)

    def _verify_reparse_consistency(self, new_content: str) -> None:
        reparsed_blocks, reparsed_trailing = find_blocks(new_content)
        round_tripped = rebuild_text(reparsed_blocks, trailing_text=reparsed_trailing)
        if round_tripped != new_content:
            raise ValueError(
                "Detected divergence after re-parsing the pending changes; save aborted to prevent data loss."
            )
        # The round-trip above is only a sanity check: the parser keeps any text it does not
        # understand, so it holds for almost anything. This is the check that can fail.
        # Edits only move '*' or change numbers, so the list of questions must be identical;
        # a merged, lost or un-commented question would still round-trip.
        def identity(blocks):
            return [(b.name, b.token, b.is_commented) for b in blocks]

        if identity(reparsed_blocks) != identity(self.blocks):
            raise ValueError(
                "The pending changes would add, remove or merge Setup Questions; save aborted to prevent data loss."
            )

    def _read_text_from_disk(self, path: Path) -> str:
        try:
            with path.open("r", encoding=self._encoding, newline="") as handle:
                return handle.read()
        except UnicodeDecodeError:
            # The file changed to something that is no longer valid in our encoding; latin-1 reads any bytes.
            try:
                with path.open("r", encoding="latin-1", newline="") as handle:
                    return handle.read()
            except OSError as exc:
                raise ValueError(str(exc)) from exc
        except OSError as exc:
            raise ValueError(str(exc)) from exc

    def _refresh_state_from_text(self, text: str) -> None:
        try:
            self.blocks, self.trailing_text = find_blocks(text)
        except re.error as exc:
            raise ValueError(f"Unable to re-parse content after save: {exc}") from exc
        self.initial_crc = extract_crc_header(text)


def describe_block(block: QuestionBlock) -> str:
    token_display = block.token or "<none>"
    help_text = format_help_text(block.help_string)
    value_match = VALUE_PATTERN.search(block.body)
    if value_match:
        info = f"Value: <{value_match.group('value')}>"
    else:
        option = selected_option(block.body)
        info = f"Selected option: {option or '<none>'}"
    description = f"Name: {block.name} | Token: {token_display} | {info}"
    if help_text:
        description = f"{description}\n  Help: {help_text}"
    return description


def format_help_text(help_string: Optional[str]) -> str:
    if not help_string:
        return ""

    help_text = " ".join(help_string.split())
    max_help_length = 120
    if len(help_text) > max_help_length:
        return help_text[: max_help_length - 3] + "..."
    return help_text


def _require_editable(block: QuestionBlock) -> None:
    if block.is_commented:
        raise ValueError(
            f"'{block.name}' is commented out in the export (inactive on this system), so it cannot be edited."
        )


def _validate_value_range(block: QuestionBlock, new_value: int) -> None:
    """Reject values the question cannot hold: explicit Min/Max, else its byte Width."""

    low, high = block.value_bounds()
    if low is not None and new_value < low:
        if block.min_value is not None:
            raise ValueError(f"Value {new_value} is below Min {low}.")
        raise ValueError(f"Value {new_value} is below {low}; this setting cannot be negative.")
    if high is not None and new_value > high:
        if block.max_value is not None:
            raise ValueError(f"Value {new_value} is above Max {high}.")
        raise ValueError(
            f"Value {new_value} is above {high}, the largest a {block.width}-byte setting can hold."
        )


def apply_changes(
    manager: NVRAMManager,
    blocks: List[QuestionBlock],
    indices: Iterable[int],
    option_label: Optional[str],
    new_value: Optional[int],
) -> List[QuestionBlock]:
    updated_blocks = blocks.copy()
    for idx in indices:
        block = blocks[idx]
        try:
            if option_label is not None:
                updated_block = manager.update_options(block, option_label)
            elif new_value is not None:
                _validate_value_range(block, new_value)
                updated_block = manager.update_value(block, new_value)
            else:
                raise ValueError("Either option_label or new_value must be provided")
        except ValueError as exc:
            raise ValueError(
                f"{exc} (block '{block.name}' starting near line {block.line_number})"
            ) from exc
        updated_blocks[idx] = updated_block
    return updated_blocks


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """Arguments of the original ``nvram_editor.py`` command (kept for existing scripts)."""

    parser = argparse.ArgumentParser(
        description="Update AMI SCE NVRAM configuration blocks. "
        "Prefer nvram_tweaker_cli.py (list / show / set / export / import / restore)."
    )
    parser.add_argument("file", type=Path, help="Path to the NVRAM configuration file")
    parser.add_argument("query", help="Question name to search (exact or partial)")
    parser.add_argument("value", help="New option label substring or numeric value, depending on the block type")
    parser.add_argument("--dry-run", action="store_true", help="Show the changes without writing anything")
    parser.add_argument("--token", help="Token (hexadecimal) to disambiguate repeated questions")
    parser.add_argument("--exact", action="store_true", help="Require an exact question name match")
    parser.add_argument("--all", action="store_true", help="Apply the change to every matching question")
    parser.add_argument(
        "--mode",
        choices=["option", "value"],
        required=True,
        help="Whether the target uses option lines (move *) or a numeric Value",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Old entry point: forwards to ``nvram_tweaker_cli.py set``."""

    args = parse_args(argv)
    from nvram_tweaker_cli import main as cli_main

    forwarded = ["set", str(args.file), args.query, args.value, "--mode", args.mode]
    for flag, enabled in (("--dry-run", args.dry_run), ("--exact", args.exact), ("--all", args.all)):
        if enabled:
            forwarded.append(flag)
    if args.token is not None:
        forwarded += ["--token", args.token]
    return cli_main(forwarded)


if __name__ == "__main__":
    raise SystemExit(main())
