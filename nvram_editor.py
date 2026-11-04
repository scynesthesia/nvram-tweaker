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
from typing import Iterable, List, Optional

from nvram_crc import extract_crc_header, preserve_crc_header
from nvram_parsing import (
    OPTION_LINE_PATTERN,
    VALUE_PATTERN,
    find_blocks,
    matches_name,
    selected_option,
    validate_numeric_input,
)
from nvram_reconstruction import rebuild_text
from nvram_safety import find_risky_blocks
from nvram_structures import OptionField, QuestionBlock, ValueField

LOGGER = logging.getLogger(__name__)


def _decode_bytes(raw_bytes: bytes, path: Path) -> tuple[str, str]:
    """Decode as UTF-8, falling back to latin-1 (which accepts every byte sequence)."""

    try:
        return raw_bytes.decode("utf-8"), "utf-8"
    except UnicodeDecodeError as exc:
        preview = raw_bytes[: exc.start].decode("utf-8", errors="ignore")
        line_number = preview.count("\n") + 1
        LOGGER.warning(
            "Decoded '%s' with latin-1 after UTF-8 failure near line %d.",
            path,
            line_number,
        )
        return raw_bytes.decode("latin-1"), "latin-1"


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

    def filter_blocks(self, query: str, exact: bool, token: Optional[str]) -> List[QuestionBlock]:
        filtered = [b for b in self.blocks if not b.is_commented and matches_name(b, query, exact)]
        if token is not None:
            filtered = [b for b in filtered if b.token == token]
        return filtered

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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update AMI SCE NVRAM configuration blocks.")
    parser.add_argument("file", type=Path, help="Path to the NVRAM configuration file")
    parser.add_argument("query", help="Question name to search (exact or partial)")
    parser.add_argument("value", help="New option label substring or numeric value, depending on the block type")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Perform all steps without writing any changes to disk",
    )
    parser.add_argument(
        "--token",
        help="Token identifier to disambiguate repeated questions",
    )
    parser.add_argument(
        "--exact",
        action="store_true",
        help="Require an exact question name match instead of partial",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Apply the change to all matching blocks; otherwise only the first is changed",
    )
    parser.add_argument(
        "--mode",
        choices=["option", "value"],
        required=True,
        help="Specify whether the target uses option lines (move *) or a numeric Value",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    try:
        manager = NVRAMManager().load_file(args.file)
        blocks = manager.blocks

        candidates = manager.filter_blocks(args.query, args.exact, args.token)

        if not candidates:
            raise ValueError("No matching Setup Question blocks found")

        target_indices = [blocks.index(b) for b in candidates]
        if len(target_indices) > 1 and not args.all and args.token is None:
            LOGGER.info("Multiple matching blocks found:")
            for display_index, block_index in enumerate(target_indices, start=1):
                description = describe_block(blocks[block_index])
                formatted_description = "\n   ".join(description.splitlines())
                LOGGER.info("%s. %s", display_index, formatted_description)
            while True:
                selection = input(
                    'Select the indices to modify (comma-separated, e.g., "1,3,5") or "A" for all: '
                ).strip()
                if selection.lower() == "a":
                    break

                try:
                    chosen_numbers = [int(part.strip()) for part in selection.split(",") if part.strip()]
                except ValueError:
                    chosen_numbers = []

                if chosen_numbers and all(1 <= num <= len(target_indices) for num in chosen_numbers):
                    unique_indices = []
                    for num in chosen_numbers:
                        block_idx = target_indices[num - 1]
                        if block_idx not in unique_indices:
                            unique_indices.append(block_idx)
                    target_indices = unique_indices
                    break
                LOGGER.info("Invalid selection. Please try again.")
        elif not args.all:
            target_indices = target_indices[:1]

        option_label: Optional[str] = args.value if args.mode == "option" else None
        new_value: Optional[int]
        value_conversion_note: Optional[str] = None

        if args.mode == "value":
            new_value, value_conversion_note = validate_numeric_input(args.value.strip())
        else:
            new_value = None

        before_descriptions = {idx: describe_block(blocks[idx]) for idx in target_indices}
        try:
            updated_blocks = apply_changes(manager, blocks, target_indices, option_label, new_value)
        except ValueError as exc:
            raise ValueError(f"Could not update the requested block(s): {exc}") from exc
        after_descriptions = {idx: describe_block(updated_blocks[idx]) for idx in target_indices}

        LOGGER.info("Summary of changes (Before -> After):")
        if value_conversion_note:
            LOGGER.info("  %s", value_conversion_note)
        for idx in target_indices:
            LOGGER.info("- %s", before_descriptions[idx])
            LOGGER.info("  -> %s", after_descriptions[idx])

        risky = find_risky_blocks(blocks[idx] for idx in target_indices)
        if risky:
            LOGGER.warning("WARNING: these questions touch settings that can leave the machine unbootable")
            LOGGER.warning("or without input/display if set wrong:")
            for block, keywords in risky:
                LOGGER.warning("  - %s [%s]", block.name, ", ".join(keywords))
            risk_confirmation = input("Type YES to confirm you want to change risky settings: ").strip()
            if risk_confirmation.lower() != "yes":
                raise ValueError("No changes applied; risky change not confirmed.")

        confirmation = input("Apply these changes? (Y/N): ").strip().lower()
        if confirmation not in {"y", "yes"}:
            raise ValueError("No changes applied; user cancelled at confirmation step.")

        manager.blocks = updated_blocks
        new_text = manager.rebuild_text(updated_blocks)
        if args.dry_run:
            LOGGER.info("[DRY RUN] No changes were written to disk.")
            return 0

        manager.save(args.file, new_text)
        return 0
    except ValueError as exc:
        LOGGER.error("%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
