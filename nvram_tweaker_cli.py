"""Command-line interface for nvram-tweaker, for technical users and scripts.

    nvram_tweaker_cli.py list    FILE [QUERY]          what is in an export
    nvram_tweaker_cli.py show    FILE QUERY            one question in full
    nvram_tweaker_cli.py set     FILE QUERY VALUE      change a setting in the file
    nvram_tweaker_cli.py export | import | restore     talk to the BIOS through SCEWIN
    nvram_tweaker_cli.py diagnose LOG                  explain a SCEWIN log

Exit codes: 0 success, 1 error or cancelled, 2 bad usage or an ambiguous match.
Nothing is written without confirmation: interactive runs ask, scripts pass
``--yes`` (and ``--allow-risky`` for settings that can lock a board).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Callable, List, Optional, Sequence

import scewin_runner
from nvram_cli_prompts import confirm_write, is_interactive
from nvram_editor import NVRAMManager, apply_changes, restore_point_path
from nvram_parsing import validate_numeric_input
from nvram_safety import current_setting, diff_blocks, noted_keywords, option_text, risky_keywords
from nvram_structures import OptionField, QuestionBlock, ValueField
from nvram_version import __version__

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2

EXAMPLES = """\
examples:
  nvram_tweaker_cli.py list nvram.txt "fan"            find questions by name
  nvram_tweaker_cli.py show nvram.txt "Fan Start" --token 2B
  nvram_tweaker_cli.py set  nvram.txt "Fan Start" 45 --dry-run
  nvram_tweaker_cli.py set  nvram.txt "ASPM Mode" "Disabled" --yes
  nvram_tweaker_cli.py set  nvram.txt "XHCI" "Enabled" --yes --allow-risky
  nvram_tweaker_cli.py export --scewin C:\\SCEWIN --file nvram.txt
  nvram_tweaker_cli.py import --scewin C:\\SCEWIN --file nvram.txt

Tokens are hexadecimal as printed in the export (7D1 and 0x7d1 are the same).
"""


def _kind(block: QuestionBlock) -> str:
    if any(isinstance(f, ValueField) for f in block.fields):
        return "value"
    if any(isinstance(f, OptionField) for f in block.fields):
        return "option"
    return "none"


def _flags(block: QuestionBlock) -> str:
    return ("!" if risky_keywords(block.name) else "") + ("~" if noted_keywords(block.name) else "")


def _error(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)


def _describe_match(block: QuestionBlock) -> str:
    return f"line {block.line_number:<6} token {block.token or '-':<6} {block.name}"


# --- list / show -----------------------------------------------------------------


def cmd_list(args: argparse.Namespace) -> int:
    manager = NVRAMManager().load_file(args.file)
    if args.query is None and args.token is None:
        indices = [i for i, b in enumerate(manager.blocks) if not b.is_commented]
    else:
        indices = manager.matching_indices(args.query or "", args.exact, args.token)

    rows = []
    for index in indices:
        block = manager.blocks[index]
        rows.append(
            {
                "line": block.line_number,
                "token": block.token,
                "name": block.name,
                "kind": _kind(block),
                "setting": current_setting(block),
                "width": block.width,
                "risk": risky_keywords(block.name),
                "note": noted_keywords(block.name),
            }
        )

    if args.json:
        print(json.dumps(rows, indent=2))
        return EXIT_OK

    if not rows:
        print("No matching questions.")
        return EXIT_OK
    for row in rows:
        flags = ("!" if row["risk"] else "") + ("~" if row["note"] else "")
        print(f"{row['line']:>6}  {row['token'] or '-':<6}  {flags:<2}  {row['setting'] or '-':<28}  {row['name']}")
    print(f"\n{len(rows)} question(s). ! = confirm before changing, ~ = worth a look (see README).")
    return EXIT_OK


def cmd_show(args: argparse.Namespace) -> int:
    manager = NVRAMManager().load_file(args.file)
    indices = manager.matching_indices(args.query, args.exact, args.token)
    if not indices:
        raise ValueError(_no_match_message(manager, args.query))

    shown = []
    for index in indices:
        block = manager.blocks[index]
        low, high = block.value_bounds()
        shown.append(
            {
                "line": block.line_number,
                "name": block.name,
                "token": block.token,
                "kind": _kind(block),
                "setting": current_setting(block),
                "width_bytes": block.width,
                "range": [low, high] if _kind(block) == "value" else None,
                "options": [option_text(f) for f in block.fields if isinstance(f, OptionField)],
                "selected": [option_text(f) for f in block.fields if isinstance(f, OptionField) and f.selected],
                "help": block.help_string,
                "risk": risky_keywords(block.name),
                "note": noted_keywords(block.name),
            }
        )

    if args.json:
        print(json.dumps(shown, indent=2))
        return EXIT_OK

    for position, item in enumerate(shown):
        if position:
            print()
        print(f"Name:     {item['name']}")
        print(f"Token:    {item['token'] or '-'}    (line {item['line']})")
        print(f"Current:  {item['setting'] or '-'}")
        if item["kind"] == "value":
            low, high = item["range"]
            size = f", {item['width_bytes']}-byte setting" if item["width_bytes"] else ""
            print(f"Range:    {low} to {high}{size}")
        for text in item["options"]:
            print(f"  {'*' if text in item['selected'] else ' '} {text}")
        if item["help"]:
            print(f"Help:     {item['help']}")
        if item["risk"]:
            print(f"Careful:  confirm-level setting ({', '.join(item['risk'])})")
        elif item["note"]:
            print(f"Note:     {', '.join(item['note'])}")
    return EXIT_OK


def _no_match_message(manager: NVRAMManager, query: str) -> str:
    inactive = [b for b in manager.blocks if b.is_commented and query.lower() in b.name.lower()]
    if inactive:
        return (
            f"No active question matches '{query}'. {len(inactive)} inactive (commented-out) "
            "variant(s) exist but cannot be edited."
        )
    return f"No question matches '{query}'."


# --- set -------------------------------------------------------------------------


def _choose(
    manager: NVRAMManager, indices: List[int], ask: Callable[[str], str]
) -> Optional[List[int]]:
    print("Several questions match:", file=sys.stderr)
    for number, index in enumerate(indices, start=1):
        block = manager.blocks[index]
        print(f"  {number}. {_describe_match(block)}  [{current_setting(block) or '-'}]", file=sys.stderr)
    while True:
        answer = ask('Which ones? (e.g. "1,3", "a" for all, "q" to cancel): ').strip().lower()
        if answer in ("q", ""):
            return None
        if answer == "a":
            return indices
        try:
            numbers = [int(part) for part in answer.split(",") if part.strip()]
        except ValueError:
            numbers = []
        if numbers and all(1 <= n <= len(indices) for n in numbers):
            return [indices[n - 1] for n in dict.fromkeys(numbers)]
        print("Invalid selection.", file=sys.stderr)


def cmd_set(
    args: argparse.Namespace, *, interactive: Optional[bool], ask: Callable[[str], str]
) -> int:
    manager = NVRAMManager().load_file(args.file)
    indices = manager.matching_indices(args.query, args.exact, args.token)
    if not indices:
        raise ValueError(_no_match_message(manager, args.query))

    if interactive is None:
        interactive = is_interactive()

    if len(indices) > 1 and not args.all:
        if interactive and not args.yes:
            chosen = _choose(manager, indices, ask)
            if chosen is None:
                print("Cancelled.", file=sys.stderr)
                return EXIT_ERROR
            indices = chosen
        else:
            _error(
                f"{len(indices)} questions match '{args.query}'. Narrow it with --token or --exact, "
                "or pass --all to change every one:"
            )
            for index in indices:
                print(f"  {_describe_match(manager.blocks[index])}", file=sys.stderr)
            return EXIT_USAGE

    updated = list(manager.blocks)
    notes: List[str] = []
    for index in indices:
        block = manager.blocks[index]
        mode = _kind(block) if args.mode == "auto" else args.mode
        if mode == "value":
            try:
                number, note = validate_numeric_input(args.value.strip())
            except ValueError as exc:
                raise ValueError(f"'{block.name}' (line {block.line_number}) needs a number: {exc}") from None
            updated = apply_changes(manager, updated, [index], None, number)
            if note and note not in notes:
                notes.append(note)
        elif mode == "option":
            updated = apply_changes(manager, updated, [index], args.value, None)
        else:
            raise ValueError(f"'{block.name}' (line {block.line_number}) has no options or number to set.")

    changes = diff_blocks(manager.blocks, updated)
    if not changes:
        print("Nothing to change: the setting already has that value.")
        return EXIT_OK

    print(f"{len(changes)} setting(s) will change:")
    for note in notes:
        print(f"  {note}")
    for change in changes:
        print(f"  - {change.format()}")

    if args.dry_run:
        print("[DRY RUN] Nothing was written.")
        return EXIT_OK

    if not confirm_write(
        "Apply these changes?",
        risky_lines=[c.format() for c in changes if c.risk_keywords],
        yes=args.yes,
        allow_risky=args.allow_risky,
        interactive=interactive,
        ask=ask,
    ):
        return EXIT_ERROR

    manager.save(args.file, manager.rebuild_text(updated))
    print(f"Saved {args.file}. Previous version: {args.file.name}.bak. Untouched original: {restore_point_path(args.file).name}.")
    return EXIT_OK


# --- entry point ------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nvram_tweaker_cli.py",
        description="Inspect and change AMI SCEWIN NVRAM exports, and read or write them through SCEWIN.",
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"nvram-tweaker {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def add_match_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--token", help="question token (hexadecimal) to tell repeated names apart")
        p.add_argument("--exact", action="store_true", help="match the whole question name, not part of it")

    list_parser = sub.add_parser("list", help="list the questions in an export")
    list_parser.add_argument("file", type=Path)
    list_parser.add_argument("query", nargs="?", help="part of the question name")
    add_match_args(list_parser)
    list_parser.add_argument("--json", action="store_true", help="machine-readable output")

    show_parser = sub.add_parser("show", help="show one question in full")
    show_parser.add_argument("file", type=Path)
    show_parser.add_argument("query", help="part of the question name")
    add_match_args(show_parser)
    show_parser.add_argument("--json", action="store_true", help="machine-readable output")

    set_parser = sub.add_parser("set", help="change a setting in the file (not the BIOS)")
    set_parser.add_argument("file", type=Path)
    set_parser.add_argument("query", help="part of the question name")
    set_parser.add_argument("value", help="a number, or part of an option's text such as Disabled")
    add_match_args(set_parser)
    set_parser.add_argument("--all", action="store_true", help="change every matching question")
    set_parser.add_argument(
        "--mode",
        choices=["auto", "option", "value"],
        default="auto",
        help="auto (default) picks per question: numbers for numeric ones, options otherwise",
    )
    set_parser.add_argument("--dry-run", action="store_true", help="show the changes, write nothing, ask nothing")
    set_parser.add_argument("--yes", action="store_true", help="skip the Y/N question (scripts)")
    set_parser.add_argument(
        "--allow-risky", action="store_true", help="confirm changes to risky settings without asking (scripts)"
    )

    scewin_runner.add_subcommands(sub)
    return parser


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    interactive: Optional[bool] = None,
    ask: Callable[[str], str] = input,
) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "list":
            return cmd_list(args)
        if args.command == "show":
            return cmd_show(args)
        if args.command == "set":
            return cmd_set(args, interactive=interactive, ask=ask)
        return scewin_runner.run(args, interactive=interactive, ask=ask)
    except (ValueError, OSError, scewin_runner.SCEWINError) as exc:
        _error(str(exc))
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
