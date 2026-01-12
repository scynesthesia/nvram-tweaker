"""Tools for updating AMI SCE NVRAM text exports.

Finds ``Setup Question`` blocks and lets you move the selected option or update
numeric ``Value = <X>`` entries. Searches can be exact or partial, filtered by
token, and applied to one or many matching blocks.
"""
from __future__ import annotations

import argparse
import logging
import mmap
import re
from collections import deque
from dataclasses import replace
from pathlib import Path
from typing import Iterable, List, Optional

from nvram_crc import _contains_crc_marker, recalculate_crc
from nvram_parsing import (
    BLOCK_PATTERN,
    HELP_PATTERN,
    MAX_PATTERN,
    MIN_PATTERN,
    NUMERIC_INPUT_PATTERN,
    OPTION_LINE_PATTERN,
    SETUP_BOUNDARY_PATTERN,
    TOKEN_PATTERN,
    TOKEN_VALIDATION_PATTERN,
    VALUE_PATTERN,
    find_blocks,
    matches_name,
    selected_option,
    validate_numeric_input,
)
from nvram_reconstruction import rebuild_text
from nvram_structures import OptionField, QuestionBlock, ValueField

LOGGER = logging.getLogger(__name__)

MMAP_READ_THRESHOLD = 4 * 1024 * 1024
TOOL_VERSION = "0.0.0-dev"

def _read_file_via_mmap(path: Path) -> bytes:
    """
    Read file contents using a memory map to limit peak memory usage for large files.
    """
    with path.open("rb") as handle:
        with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as mm_obj:
            return mm_obj.read()


def _decode_bytes(raw_bytes: bytes, path: Path) -> tuple[str, str]:
    try:
        return raw_bytes.decode("utf-8"), "utf-8"
    except UnicodeDecodeError as exc:
        try:
            text = raw_bytes.decode("latin-1")
        except UnicodeDecodeError:
            preview = raw_bytes[: exc.start].decode("utf-8", errors="ignore")
            line_number = preview.count("\n") + 1
            raise ValueError(
                f"Failed to decode '{path}' as UTF-8 or latin-1 near line {line_number}: {exc.reason}."
            ) from exc

        preview = raw_bytes[: exc.start].decode("utf-8", errors="ignore")
        line_number = preview.count("\n") + 1
        LOGGER.warning(
            "Decoded '%s' with latin-1 after UTF-8 failure near line %d.",
            path,
            line_number,
        )
        return text, "latin-1"


class NVRAMManager:
    def __init__(self) -> None:
        self.path: Optional[Path] = None
        self.original_text: Optional[str] = None
        self.blocks: List[QuestionBlock] = []
        self.trailing_text: str = ""
        self.initial_crc: Optional[str] = None
        self.has_crc_marker: bool = False
        self.line_ending: str = "\n"
        self._backup_via_copy: bool = False
        self._encoding: str = "utf-8"
        self.last_saved_text: Optional[str] = None
        self.undo_stack: deque[str] = deque(maxlen=5)

    def load_file(self, path: Path) -> "NVRAMManager":
        if not path.exists():
            raise ValueError(f"The file '{path}' does not exist. Please provide a valid path.")

        file_size = path.stat().st_size
        use_mmap = file_size >= MMAP_READ_THRESHOLD
        try:
            if use_mmap:
                raw_bytes = _read_file_via_mmap(path)
                self._backup_via_copy = True
            else:
                raw_bytes = path.read_bytes()
                self._backup_via_copy = False
        except OSError as exc:
            raise ValueError(f"Unable to read '{path}': {exc}") from exc

        text, self._encoding = _decode_bytes(raw_bytes, path)

        try:
            self.blocks, self.trailing_text = find_blocks(text)
        except re.error as exc:
            raise ValueError(f"Unable to parse Setup Question blocks: {exc}") from exc

        self.path = path
        self.original_text = None if self._backup_via_copy else text
        self.last_saved_text = text
        self.undo_stack.clear()
        self.initial_crc = self._extract_crc_header(text)
        self.has_crc_marker = _contains_crc_marker(text)
        self.line_ending = "\r\n" if "\r\n" in text else "\n"
        return self

    @staticmethod
    def _extract_crc_header(text: str) -> Optional[str]:
        lines = text.splitlines()
        first_line = lines[0].strip() if lines else ""
        if first_line.startswith("HIICrc32"):
            return first_line
        return None

    def _normalize_newlines(self, content: str) -> str:
        content = content.replace("\r\n", "\n").replace("\r", "\n")
        if self.line_ending == "\n":
            return content
        return self.line_ending.join(content.split("\n"))

    def update_value(self, block: QuestionBlock, new_value: int) -> QuestionBlock:
        def replace_match(match: re.Match[str]) -> str:
            return f"Value = <{new_value}>"

        body = block.body
        if not VALUE_PATTERN.search(body):
            raise ValueError("Block does not contain a Value line")
        new_body = VALUE_PATTERN.sub(replace_match, body, count=1)
        new_fields: List[OptionField | ValueField] = []
        for field in block.fields:
            if isinstance(field, ValueField):
                new_fields.append(replace(field, value=new_value))
            else:
                new_fields.append(field)
        return block.with_body(new_body).with_fields(new_fields)

    def update_options(self, block: QuestionBlock, option_label: str) -> QuestionBlock:
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
            line_ending = "\r\n" if line.endswith("\r\n") else ("\n" if line.endswith("\n") else "")

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
        filtered = [b for b in self.blocks if matches_name(b, query, exact)]
        if token is not None:
            filtered = [b for b in filtered if b.token == token]
        return filtered

    def save(
        self,
        path: Path,
        new_content: str,
        *,
        bypass_crc: bool = False,
        bypass_crc_mode: str = "empty",
        force_crc_bypass: bool = False,
    ) -> None:
        if (
            bypass_crc
            and not force_crc_bypass
            and bypass_crc_mode in {"remove", "empty"}
            and self.has_crc_marker
        ):
            raise ValueError(
                "Unsafe CRC bypass blocked: refusing to remove or empty HIICrc32 when a CRC marker is present. "
                "Enable force mode to proceed."
            )
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

        backup_path = path.with_name(path.name + ".bak")
        backup_path.write_text(current_disk_text, encoding=self._encoding)
        self.undo_stack.append(current_disk_text)

        updated_content = recalculate_crc(
            new_content,
            self.initial_crc,
            bypass=bypass_crc,
            bypass_mode=bypass_crc_mode,
            tool_version=TOOL_VERSION,
        )
        updated_content = self._normalize_newlines(updated_content)
        path.write_text(updated_content, encoding=self._encoding, newline="")
        self.last_saved_text = updated_content
        self._refresh_state_from_text(updated_content)

    def rollback_last_save(self) -> None:
        if self.path is None:
            raise ValueError("No file is currently loaded; cannot roll back.")

        if not self.undo_stack:
            raise ValueError("No prior save is available to roll back to.")

        previous_content = self.undo_stack.pop()
        self.path.write_text(previous_content, encoding=self._encoding, newline="")
        self.last_saved_text = previous_content
        self._refresh_state_from_text(previous_content)

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

    def _read_text_from_disk(self, path: Path) -> str:
        try:
            with path.open("r", encoding=self._encoding, newline="") as handle:
                return handle.read()
        except UnicodeDecodeError:
            try:
                with path.open("r", encoding="latin-1", newline="") as handle:
                    return handle.read()
            except (UnicodeDecodeError, OSError) as exc:
                raise ValueError(str(exc)) from exc
        except OSError as exc:
            raise ValueError(str(exc)) from exc

    def _refresh_state_from_text(self, text: str) -> None:
        try:
            self.blocks, self.trailing_text = find_blocks(text)
        except re.error as exc:
            raise ValueError(f"Unable to re-parse content after save/rollback: {exc}") from exc
        self.initial_crc = self._extract_crc_header(text)
        self.has_crc_marker = _contains_crc_marker(text)
        self.line_ending = "\r\n" if "\r\n" in text else "\n"


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


def _validate_value_range(block: QuestionBlock, new_value: int) -> None:
    if block.min_value is not None and new_value < block.min_value:
        raise ValueError(f"Value {new_value} is below Min {block.min_value}.")
    if block.max_value is not None and new_value > block.max_value:
        raise ValueError(f"Value {new_value} is above Max {block.max_value}.")


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


def _validate_value_arg(raw_value: str) -> tuple[int, Optional[str]]:
    try:
        return validate_numeric_input(raw_value)
    except ValueError as exc:
        raise ValueError(str(exc))


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
            raw_value = args.value.strip()
            try:
                new_value, value_conversion_note = _validate_value_arg(raw_value)
            except ValueError as exc:
                raise ValueError(str(exc)) from exc
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
