from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple, Union

import dearpygui.dearpygui as dpg
from tkinter import Tk, filedialog

from nvram_editor import NVRAMManager, apply_changes
from nvram_parsing import OPTION_LINE_PATTERN, VALUE_PATTERN, validate_numeric_input
from nvram_safety import current_setting, find_risky_blocks
from nvram_structures import QuestionBlock
from scewin_runner import (
    DEFAULT_SCRIPT_NAME,
    SCEWINError,
    SCEWINResult,
    SCEWINRunner,
    default_scewin_candidates,
    guess_scewin_dir,
    restore_point_path,
)


def resource_path(relative_path: str) -> str:
    base_path = getattr(sys, "_MEIPASS", Path(__file__).parent)
    return str(Path(base_path) / relative_path)


INLINE_COMMENT_PATTERN = re.compile(r"\s//")
INT32_MAX = 2**31 - 1
DEFAULT_VIEWPORT_WIDTH = 1200
DEFAULT_VIEWPORT_HEIGHT = 840
SIDEBAR_WIDTH_MIN = 320
SIDEBAR_WIDTH_RATIO = 0.32
SIDEBAR_WIDTH_MAX = 520
GUTTER_MIN = 48
GUTTER_RATIO = 0.08
GUTTER_MAX = 120
DETAIL_WIDTH_MIN = 420
WRAP_PADDING = 40
WRAP_RATIO = 0.7
CONSOLE_HEIGHT_MIN = 150
CONSOLE_HEIGHT_RATIO = 0.22
CONSOLE_HEIGHT_MAX = 240
LAYOUT_PADDING = 110
AVAILABLE_HEIGHT_MIN = 420
CONTROL_REGION_MIN = 220
CONTROL_REGION_RATIO = 0.44
SIDEBAR_LIST_MIN = 240
SIDEBAR_LIST_RATIO = 0.55


@dataclass
class OptionEntry:
    display: str
    match_value: str
    selected: bool


@dataclass
class PendingChange:
    block_indices: List[int]
    action: str
    option_match_value: Optional[str] = None
    option_display: Optional[str] = None
    new_value: Optional[int] = None
    conversion_note: Optional[str] = None


@dataclass
class GUIState:
    manager: NVRAMManager = field(default_factory=NVRAMManager)
    blocks: List[QuestionBlock] = field(default_factory=list)
    original_blocks: List[QuestionBlock] = field(default_factory=list)
    label_to_index: Dict[str, int] = field(default_factory=dict)
    selected_index: Optional[int] = None
    selected_indices: List[int] = field(default_factory=list)
    log_messages: List[str] = field(default_factory=list)
    header_font: Optional[int] = None
    body_font: Optional[int] = None
    mono_font: Optional[int] = None
    pending_change: Optional["PendingChange"] = None
    modified_indices: Set[int] = field(default_factory=set)
    batch_list_theme: Optional[int] = None
    select_all_flash_theme: Optional[int] = None
    modified_sidebar_theme: Optional[int] = None
    scewin_runner: Optional[SCEWINRunner] = None
    pending_scewin_action: Optional[str] = None
    pending_scewin_path: Optional[Path] = None
    closing: bool = False


state = GUIState()


def append_log(message: str) -> None:
    state.log_messages.append(message)
    state.log_messages = state.log_messages[-400:]
    if dpg.does_item_exist("log_output"):
        dpg.set_value("log_output", "\n".join(state.log_messages))


def bind_mono_font(tag: str) -> None:
    if state.mono_font and dpg.does_item_exist(tag):
        dpg.bind_item_font(tag, state.mono_font)


def extract_options(body: str) -> List[OptionEntry]:
    options: List[OptionEntry] = []
    for line in body.splitlines():
        opt_match = OPTION_LINE_PATTERN.match(line)
        if not opt_match:
            continue

        code = opt_match.group("code")
        label = opt_match.group("label")
        cleaned_label = INLINE_COMMENT_PATTERN.split(label, maxsplit=1)[0].strip() or label.strip()
        cleaned_display = f"{cleaned_label} [{code}]"
        match_value = f"[{code}]{cleaned_label}"
        options.append(
            OptionEntry(display=cleaned_display, match_value=match_value, selected=bool(opt_match.group("star")))
        )
    return options


def _describe_block_setting(block: Optional[QuestionBlock]) -> str:
    if block is None:
        return "<unknown>"
    return current_setting(block) or "No selection"


def set_detail_header(block: QuestionBlock) -> None:
    name_text = block.name
    token_text = f"Token: {block.token or 'None'}"
    dpg.set_value("question_name", name_text)
    dpg.set_value("question_token", token_text)
    dpg.set_value("help_text", block.help_string or "No help text available for this question.")


def set_batch_header(count: int) -> None:
    dpg.set_value("question_name", "Batch Mode")
    dpg.set_value("question_token", "Multiple selections")
    dpg.set_value("help_text", f"Editing {count} selected questions.")


def update_select_all_button_label(count: int) -> None:
    if not dpg.does_item_exist("select_all_filtered_button"):
        return

    label_suffix = f" ({count} found)" if count >= 0 else ""
    dpg.configure_item("select_all_filtered_button", label=f"Select All Filtered{label_suffix}")


def ensure_batch_list_theme() -> Optional[int]:
    if state.batch_list_theme is not None:
        return state.batch_list_theme

    with dpg.theme() as theme:
        with dpg.theme_component(dpg.mvChildWindow):
            dpg.add_theme_color(dpg.mvThemeCol_ChildBg, (50, 100, 120, 70))
            dpg.add_theme_color(dpg.mvThemeCol_Border, (90, 180, 210, 160))
            dpg.add_theme_style(dpg.mvStyleVar_ChildRounding, 8)
            dpg.add_theme_style(dpg.mvStyleVar_WindowPadding, 12, 10)

    state.batch_list_theme = theme
    return theme


def ensure_select_all_flash_theme() -> Optional[int]:
    if state.select_all_flash_theme is not None:
        return state.select_all_flash_theme

    with dpg.theme() as theme:
        with dpg.theme_component(dpg.mvButton):
            dpg.add_theme_color(dpg.mvThemeCol_Button, (60, 140, 170, 255))
            dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (70, 165, 200, 255))
            dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (55, 125, 150, 255))
            dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, 8)
            dpg.add_theme_style(dpg.mvStyleVar_FramePadding, 12, 9)

    state.select_all_flash_theme = theme
    return theme


def ensure_modified_sidebar_theme() -> Optional[int]:
    if state.modified_sidebar_theme is not None:
        return state.modified_sidebar_theme

    with dpg.theme() as theme:
        with dpg.theme_component(dpg.mvSelectable):
            dpg.add_theme_color(dpg.mvThemeCol_Text, (200, 240, 210, 255))
            dpg.add_theme_color(dpg.mvThemeCol_Header, (60, 110, 85, 120))
            dpg.add_theme_color(dpg.mvThemeCol_HeaderActive, (70, 135, 95, 170))
            dpg.add_theme_color(dpg.mvThemeCol_HeaderHovered, (80, 150, 110, 190))
            dpg.add_theme_style(dpg.mvStyleVar_FramePadding, 10, 8)

    state.modified_sidebar_theme = theme
    return theme


def flash_select_all_button() -> None:
    if not dpg.does_item_exist("select_all_filtered_button"):
        return

    theme = ensure_select_all_flash_theme()
    if theme is None:
        return

    dpg.bind_item_theme("select_all_filtered_button", theme)

    def _reset_theme(sender: Optional[int] = None, app_data: Optional[object] = None) -> None:
        if dpg.does_item_exist("select_all_filtered_button"):
            dpg.bind_item_theme("select_all_filtered_button", None)

    current_frame = dpg.get_frame_count()
    dpg.set_frame_callback(current_frame + 45, _reset_theme)


def update_batch_indicator() -> None:
    if not dpg.does_item_exist("question_list_container"):
        return

    multi_select = len(state.selected_indices) > 1
    if dpg.does_item_exist("batch_alert_label"):
        dpg.configure_item("batch_alert_label", show=multi_select)
        if multi_select:
            dpg.set_value(
                "batch_alert_label",
                f"Batch mode active: applying changes to {len(state.selected_indices)} questions.",
            )

    if multi_select:
        theme = ensure_batch_list_theme()
        if theme is not None:
            dpg.bind_item_theme("question_list_container", theme)
    else:
        dpg.bind_item_theme("question_list_container", None)


def get_filtered_indices(filter_text: str) -> List[int]:
    def _normalize(text: str) -> str:
        collapsed = text.replace("-", " ").replace("_", " ").lower()
        return " ".join(collapsed.split())

    # Commented-out questions are inactive variants, not settings: never list them.
    active_indices = [idx for idx, block in enumerate(state.blocks) if not block.is_commented]
    normalized_filter = _normalize(filter_text)
    if not normalized_filter:
        return active_indices

    words = normalized_filter.split()
    normalized_names = [_normalize(block.name) for block in state.blocks]
    matching_indices: Set[int] = set(active_indices)

    for word in words:
        word_matches = {idx for idx, name in enumerate(normalized_names) if word in name}
        if not word_matches:
            append_log(f"No results found because the word '{word}' is missing from all question names.")
            return []
        matching_indices &= word_matches

    if not matching_indices:
        append_log("No results found containing all search words in a single question name.")
        return []

    return sorted(matching_indices)


def rebuild_controls(block_index: int, block: QuestionBlock) -> None:
    if not dpg.does_item_exist("control_region"):
        return

    dpg.delete_item("control_region", children_only=True)

    value_match = VALUE_PATTERN.search(block.body)
    if value_match:
        current_value_text = value_match.group("value")
        low, high = block.value_bounds()
        # Dear PyGui's integer widget is 32-bit signed; wider settings use the text field.
        fits_int32 = (high is None or high <= INT32_MAX) and (low is None or low >= 0)
        use_numeric_input = current_value_text.isdigit() and fits_int32

        with dpg.group(parent="control_region", horizontal=True):
            if use_numeric_input:
                input_tag = "value_input_int"
                dpg.add_input_int(
                    tag=input_tag,
                    label="Value",
                    min_value=low if low is not None else 0,
                    max_value=high if high is not None else INT32_MAX,
                    min_clamped=True,
                    max_clamped=True,
                    default_value=int(current_value_text),
                    width=180,
                )
                bind_mono_font(input_tag)
            else:
                input_tag = "value_input_text"
                dpg.add_input_text(
                    tag=input_tag,
                    label="Value",
                    default_value=current_value_text,
                    width=180,
                )
                bind_mono_font(input_tag)

            dpg.add_button(
                label="Update value",
                callback=on_value_change,
                user_data={"block_index": block_index, "input_tag": input_tag},
            )
        if low is not None or high is not None:
            size = f" ({block.width}-byte setting)" if block.width else ""
            dpg.add_text(f"Allowed range: {low} to {high}{size}.", parent="control_region", tag="value_range_hint")
        dpg.add_text(
            "Enter decimal values or 0x-prefixed hexadecimal when using the text field.",
            parent="control_region",
        )
        return

    options = extract_options(block.body)
    if not options:
        dpg.add_text("No options or numeric values detected for this question.", parent="control_region")
        return

    selected_display: Optional[str] = None
    display_to_match: Dict[str, str] = {}
    for opt in options:
        display_label = f"* {opt.display}" if opt.selected else opt.display
        display_to_match[display_label] = opt.match_value
        if opt.selected:
            selected_display = display_label

    dpg.add_text("Choose an option:", parent="control_region")
    dpg.add_radio_button(
        tag="option_selector",
        items=[label for label in display_to_match.keys()],
        default_value=selected_display,
        callback=on_option_change,
        user_data={"block_index": block_index, "map": display_to_match},
        parent="control_region",
        horizontal=False,
    )


def rebuild_batch_controls() -> None:
    if not dpg.does_item_exist("control_region"):
        return

    dpg.delete_item("control_region", children_only=True)
    count = len(state.selected_indices)
    dpg.add_text(f"Apply the same change to {count} selected questions.", parent="control_region")
    dpg.add_separator(parent="control_region")

    selected_names = [state.blocks[idx].name for idx in state.selected_indices[:5]]
    if selected_names:
        dpg.add_text("Previewing first selected blocks:", parent="control_region")
        dpg.add_text("\n".join(f"- {name}" for name in selected_names), parent="control_region")
        if len(state.selected_indices) > 5:
            dpg.add_text(
                f"...and {len(state.selected_indices) - 5} more.",
                parent="control_region",
                color=(200, 200, 200),
            )
        dpg.add_separator(parent="control_region")

    with dpg.group(parent="control_region", horizontal=True):
        dpg.add_input_text(tag="batch_option_input", label="Option substring", width=220)
        dpg.add_button(label="Apply option to all", callback=on_batch_option_apply)

    with dpg.group(parent="control_region", horizontal=True):
        dpg.add_input_text(
            tag="batch_value_input",
            label="Value (decimal or 0x...)",
            width=220,
        )
        dpg.add_button(label="Apply value to all", callback=on_batch_value_apply)
        bind_mono_font("batch_value_input")

    dpg.add_text(
        "Numeric values accept decimal or 0x-prefixed hexadecimal. Option updates match by substring.",
        parent="control_region",
        wrap=460,
    )


def render_selection() -> None:
    if len(state.selected_indices) > 1:
        set_batch_header(len(state.selected_indices))
        rebuild_batch_controls()
    elif state.selected_indices:
        idx = state.selected_indices[0]
        block = state.blocks[idx]
        set_detail_header(block)
        rebuild_controls(idx, block)
    update_detail_wraps()


def update_question_list(filter_text: str = "") -> None:
    state.label_to_index = {}
    filtered_indices = get_filtered_indices(filter_text)
    update_select_all_button_label(len(filtered_indices))

    if not dpg.does_item_exist("question_list_container"):
        return

    dpg.delete_item("question_list_container", children_only=True)
    if not filtered_indices:
        if not filter_text.strip():
            if state.blocks:
                dpg.add_text("No questions available in this file.", parent="question_list_container")
            else:
                dpg.add_text("No file loaded. Open an NVRAM export to view questions.", parent="question_list_container")
            append_log("No questions are available to display.")
        else:
            dpg.add_text("No questions match the current filter.", parent="question_list_container")
        return

    modified_theme = ensure_modified_sidebar_theme()
    chosen_index: Optional[int] = None
    label_by_index: Dict[int, str] = {}

    for idx in filtered_indices:
        block = state.blocks[idx]
        clean_name = block.name.replace("\r", "").strip()
        label = f"{clean_name} (Token: {block.token or 'None'}) #{idx + 1}"
        if idx in state.modified_indices:
            label = f"* {label}"
        label_by_index[idx] = label
        state.label_to_index[label] = idx
        selectable_tag = f"question_item_{idx}"
        dpg.add_selectable(
            tag=selectable_tag,
            label=label,
            callback=on_question_selected,
            user_data={"index": idx, "label": label, "tag": selectable_tag},
            parent="question_list_container",
        )
        if idx in state.modified_indices and modified_theme is not None:
            dpg.bind_item_theme(selectable_tag, modified_theme)

    selection_set = [idx for idx in state.selected_indices if idx in filtered_indices]
    if not selection_set:
        selection_set = [filtered_indices[0]]
        state.selected_index = selection_set[0]
        state.selected_indices = [selection_set[0]]

    for idx in selection_set:
        tag = f"question_item_{idx}"
        if dpg.does_item_exist(tag):
            dpg.set_value(tag, True)

    if len(selection_set) == 1:
        chosen_index = selection_set[0]
        on_question_selected(
            None,
            None,
            {
                "index": chosen_index,
                "label": label_by_index.get(chosen_index, ""),
                "tag": f"question_item_{chosen_index}",
            },
        )

    update_batch_indicator()


def _get_viewport_dimensions() -> Tuple[int, int]:
    viewport_client_width = getattr(dpg, "get_viewport_client_width", None)
    viewport_client_height = getattr(dpg, "get_viewport_client_height", None)
    viewport_width = viewport_client_width() if viewport_client_width else 0
    viewport_height = viewport_client_height() if viewport_client_height else 0

    if not viewport_width:
        viewport_width = dpg.get_viewport_width() if hasattr(dpg, "get_viewport_width") else 0
    if not viewport_height:
        viewport_height = dpg.get_viewport_height() if hasattr(dpg, "get_viewport_height") else 0

    if not viewport_width:
        viewport_width = DEFAULT_VIEWPORT_WIDTH
    if not viewport_height:
        viewport_height = DEFAULT_VIEWPORT_HEIGHT

    return int(viewport_width), int(viewport_height)


def _calculate_layout_metrics() -> Dict[str, int]:
    viewport_width, viewport_height = _get_viewport_dimensions()

    sidebar_width = int(max(SIDEBAR_WIDTH_MIN, min(viewport_width * SIDEBAR_WIDTH_RATIO, SIDEBAR_WIDTH_MAX)))
    gutter = int(max(GUTTER_MIN, min(viewport_width * GUTTER_RATIO, GUTTER_MAX)))
    detail_width = max(DETAIL_WIDTH_MIN, viewport_width - sidebar_width - gutter)
    wrap_width = int(max(DETAIL_WIDTH_MIN, min(detail_width - WRAP_PADDING, viewport_width * WRAP_RATIO)))
    wrap_width = min(wrap_width, int(detail_width))

    console_panel_height = int(
        max(CONSOLE_HEIGHT_MIN, min(viewport_height * CONSOLE_HEIGHT_RATIO, CONSOLE_HEIGHT_MAX))
    )
    available_height = max(AVAILABLE_HEIGHT_MIN, viewport_height - console_panel_height - LAYOUT_PADDING)
    control_region_height = max(CONTROL_REGION_MIN, int(available_height * CONTROL_REGION_RATIO))
    sidebar_list_height = max(SIDEBAR_LIST_MIN, int(available_height * SIDEBAR_LIST_RATIO))

    return {
        "viewport_width": viewport_width,
        "viewport_height": viewport_height,
        "sidebar_width": sidebar_width,
        "gutter": gutter,
        "detail_width": int(detail_width),
        "wrap_width": wrap_width,
        "console_panel_height": console_panel_height,
        "available_height": int(available_height),
        "control_region_height": int(control_region_height),
        "sidebar_list_height": int(sidebar_list_height),
    }


def update_detail_wraps() -> None:
    layout = _calculate_layout_metrics()
    content_width = layout["detail_width"]
    wrap_width = layout["wrap_width"]
    if dpg.does_item_exist("question_name"):
        dpg.configure_item("question_name", wrap=wrap_width)
    if dpg.does_item_exist("question_token"):
        dpg.configure_item("question_token", wrap=wrap_width)
    if dpg.does_item_exist("help_text"):
        dpg.configure_item("help_text", width=wrap_width)
    if dpg.does_item_exist("control_region"):
        dpg.configure_item("control_region", width=content_width)
    if dpg.does_item_exist("sidebar"):
        dpg.configure_item("sidebar", height=layout["available_height"], width=layout["sidebar_width"])
    if dpg.does_item_exist("batch_alert_label"):
        sidebar_wrap = max(260, layout["sidebar_width"] - 40)
        dpg.configure_item("batch_alert_label", wrap=sidebar_wrap)
    if dpg.does_item_exist("editor_panel"):
        dpg.configure_item("editor_panel", height=layout["available_height"])
    if dpg.does_item_exist("control_region"):
        dpg.configure_item("control_region", height=layout["control_region_height"])
    if dpg.does_item_exist("question_list_container"):
        dpg.configure_item(
            "question_list_container",
            height=layout["sidebar_list_height"],
            width=layout["sidebar_width"] - 8,
        )
    if dpg.does_item_exist("console_panel"):
        dpg.configure_item("console_panel", height=layout["console_panel_height"])


def on_viewport_resize(sender: int, app_data: object) -> None:
    update_detail_wraps()


def on_search(sender: int, app_data: str) -> None:
    filter_text = app_data.strip()
    state.selected_index = None
    state.selected_indices = []
    if dpg.does_item_exist("question_list_container"):
        dpg.delete_item("question_list_container", children_only=True)
    update_question_list(filter_text)


def on_clear_selection(sender: int, app_data: object) -> None:
    filter_text = ""
    if dpg.does_item_exist("search_input"):
        filter_text = dpg.get_value("search_input") or ""

    state.selected_index = None
    state.selected_indices = []
    update_question_list(filter_text)
    append_log("Selection cleared. Returning to single-question editing.")


def on_select_all_filtered(sender: int, app_data: object) -> None:
    filter_text = ""
    if dpg.does_item_exist("search_input"):
        filter_text = dpg.get_value("search_input") or ""

    indices = get_filtered_indices(filter_text)
    if not indices:
        append_log("No questions match the current filter for batch selection.")
        return

    state.selected_indices = indices
    state.selected_index = indices[0]
    update_question_list(filter_text)
    render_selection()
    update_batch_indicator()
    append_log(f"Batch mode active: editing {len(indices)} questions.")
    append_log(f"Selected {len(indices)} questions matching the current filter.")


def on_question_selected(sender: Optional[int], app_data: object, user_data: Optional[Dict[str, object]] = None) -> None:
    block_index: Optional[int] = None
    item_tag: Optional[str] = None

    if user_data:
        block_index = int(user_data.get("index", -1)) if user_data.get("index") is not None else None
        item_tag = str(user_data.get("tag")) if user_data.get("tag") is not None else None

    if block_index is None and isinstance(app_data, str):
        block_index = state.label_to_index.get(app_data)

    if block_index is None:
        return

    state.selected_index = block_index
    state.selected_indices = [block_index]

    if dpg.does_item_exist("question_list_container"):
        for child in dpg.get_item_children("question_list_container", 1) or []:
            if item_tag and child == item_tag:
                dpg.set_value(child, True)
            else:
                dpg.set_value(child, False)

    block = state.blocks[block_index]
    render_selection()
    update_batch_indicator()
    append_log(f"Selected question: {block.name} (Token: {block.token or 'None'}).")


def on_option_change(sender: int, app_data: str, user_data: Dict[str, object]) -> None:
    if state.selected_index is None:
        return

    block_index = int(user_data["block_index"])
    match_map: Dict[str, str] = user_data["map"]  # type: ignore[assignment]
    match_value = match_map.get(app_data)
    if match_value is None:
        append_log("Could not find the selected option mapping.")
        return

    pending = PendingChange(
        block_indices=[block_index],
        action="option",
        option_match_value=match_value,
        option_display=app_data,
    )
    handle_change_request(pending)


def on_batch_option_apply(sender: int, app_data: object, user_data: Optional[Dict[str, object]] = None) -> None:
    if len(state.selected_indices) < 2:
        append_log("Batch mode requires at least two selected questions.")
        return

    option_label = dpg.get_value("batch_option_input") if dpg.does_item_exist("batch_option_input") else ""
    if not option_label or not str(option_label).strip():
        append_log("Please enter an option substring before applying.")
        return

    eligible_indices = []
    skipped_names = []
    for idx in state.selected_indices:
        block = state.blocks[idx]
        if OPTION_LINE_PATTERN.search(block.body):
            eligible_indices.append(idx)
        else:
            skipped_names.append(block.name)

    for name in skipped_names:
        append_log(f"Ignoring '{name}': block does not support option changes.")

    if not eligible_indices:
        append_log("No compatible blocks to update with the provided option.")
        return

    pending = PendingChange(
        block_indices=eligible_indices,
        action="option",
        option_match_value=str(option_label).strip(),
        option_display=str(option_label).strip(),
    )
    handle_change_request(pending)


def parse_numeric_input(block: QuestionBlock, raw_value: Union[str, int]) -> Tuple[Optional[int], Optional[str]]:
    conversion_note: Optional[str] = None

    if isinstance(raw_value, int):
        parsed_value = raw_value
    else:
        text_value = raw_value.strip()
        if not text_value:
            return None, "Please enter a value before updating."

        try:
            parsed_value, conversion_note = validate_numeric_input(text_value)
        except ValueError as exc:
            return None, str(exc)

    low, high = block.value_bounds()
    if (low is not None and parsed_value < low) or (high is not None and parsed_value > high):
        return None, _range_error_message(low, high)

    return parsed_value, conversion_note


def on_batch_value_apply(sender: int, app_data: object, user_data: Optional[Dict[str, object]] = None) -> None:
    if len(state.selected_indices) < 2:
        append_log("Batch mode requires at least two selected questions.")
        return

    if not dpg.does_item_exist("batch_value_input"):
        append_log("Batch value input not available.")
        return

    raw_value = dpg.get_value("batch_value_input")
    eligible_indices: List[int] = []
    skipped: List[Tuple[str, str]] = []
    conversion_note: Optional[str] = None
    parsed_value: Optional[int] = None

    for idx in state.selected_indices:
        block = state.blocks[idx]
        if not VALUE_PATTERN.search(block.body):
            skipped.append((block.name, "block does not support numeric values."))
            continue

        value, note = parse_numeric_input(block, raw_value)
        if value is None:
            skipped.append((block.name, note or "Invalid value for this block."))
            continue

        eligible_indices.append(idx)
        parsed_value = value
        if note and conversion_note is None:
            conversion_note = note

    for name, reason in skipped:
        append_log(f"Ignoring '{name}': {reason}")

    if parsed_value is None or not eligible_indices:
        append_log("No compatible blocks to update with the provided value.")
        return

    pending = PendingChange(
        block_indices=eligible_indices,
        action="value",
        new_value=parsed_value,
        conversion_note=conversion_note,
    )
    handle_change_request(pending)


def _range_error_message(min_value: Optional[int], max_value: Optional[int]) -> str:
    return f"Value out of range (allowed {min_value} to {max_value})"


def handle_change_request(change: "PendingChange") -> None:
    risky = find_risky_blocks(state.blocks[idx] for idx in change.block_indices)
    if not risky:
        apply_pending_change(change)
        return

    state.pending_change = change
    lines = [
        "These questions touch settings that can leave the machine unbootable or without",
        "input/display if set wrong. Make sure you have a way to clear CMOS before importing.",
        "",
    ]
    for block, keywords in risky[:15]:
        lines.append(f"- {block.name} [{', '.join(keywords)}]")
    if len(risky) > 15:
        lines.append(f"...and {len(risky) - 15} more.")
    dpg.set_value("risk_confirm_text", "\n".join(lines))
    dpg.configure_item("risk_confirm_modal", show=True)
    append_log(f"Risky change requested for {len(risky)} question(s); waiting for confirmation.")


def on_risk_confirm(sender: int, app_data: object) -> None:
    dpg.configure_item("risk_confirm_modal", show=False)
    change = state.pending_change
    state.pending_change = None
    if change is None:
        return
    append_log("Risky change confirmed by user.")
    apply_pending_change(change)


def on_risk_cancel(sender: int, app_data: object) -> None:
    dpg.configure_item("risk_confirm_modal", show=False)
    state.pending_change = None
    append_log("Risky change cancelled; nothing was modified.")
    # Rebuild the controls so the radio button snaps back to the real current option.
    render_selection()


def apply_pending_change(change: "PendingChange") -> None:
    successful = 0
    for idx in change.block_indices:
        block = state.blocks[idx]
        try:
            updated_blocks = apply_changes(
                state.manager,
                state.blocks,
                [idx],
                change.option_match_value if change.action == "option" else None,
                change.new_value if change.action == "value" else None,
            )
        except ValueError as exc:
            append_log(f"Skipped '{block.name}': {exc}")
            continue

        updated_block = updated_blocks[idx]
        state.blocks[idx] = updated_block
        state.manager.blocks[idx] = updated_block
        state.modified_indices.add(idx)
        successful += 1

    if successful:
        if change.action == "option":
            append_log(f"Updated option to '{change.option_display}' for {successful} block(s).")
        else:
            log_message = f"Updated numeric value to {change.new_value} for {successful} block(s)."
            if change.conversion_note:
                log_message = f"{log_message} ({change.conversion_note})"
            append_log(log_message)
    else:
        append_log("No blocks were updated.")

    state.pending_change = None
    if len(state.selected_indices) > 1:
        set_batch_header(len(state.selected_indices))
        rebuild_batch_controls()
    elif state.selected_indices:
        idx = state.selected_indices[0]
        rebuild_controls(idx, state.blocks[idx])

    if dpg.does_item_exist("search_input"):
        update_question_list(dpg.get_value("search_input") or "")


def on_value_change(sender: int, app_data: object, user_data: Dict[str, object]) -> None:
    block_index = int(user_data["block_index"])
    input_tag = str(user_data["input_tag"])
    if not dpg.does_item_exist(input_tag):
        return

    raw_value = dpg.get_value(input_tag)
    block = state.blocks[block_index]
    parsed_value, conversion_note = parse_numeric_input(block, raw_value)
    if parsed_value is None:
        append_log(conversion_note or "Unable to read the provided value.")
        return

    pending = PendingChange(
        block_indices=[block_index],
        action="value",
        new_value=parsed_value,
        conversion_note=conversion_note,
    )
    handle_change_request(pending)


def on_save(sender: int, app_data: object) -> None:
    if not state.manager.path:
        append_log("No file loaded. Load a file before saving changes.")
        return

    total_changes = len(state.modified_indices)
    summary = f"You are about to modify {total_changes} option(s). Do you want to continue?"

    if state.modified_indices:
        lines = ["", "Changes to be saved:"]
        for idx in sorted(state.modified_indices):
            current = state.blocks[idx]
            original = state.original_blocks[idx] if idx < len(state.original_blocks) else None
            before = _describe_block_setting(original)
            after = _describe_block_setting(current)
            token_display = current.token or "None"
            lines.append(f"- {current.name} (Token: {token_display})")
            lines.append(f"  Before: {before}")
            lines.append(f"  After:  {after}")
        summary = f"{summary}\n" + "\n".join(lines)

    dpg.set_value("save_confirm_text", summary)
    dpg.configure_item("save_confirm_modal", show=True)


def _hide_save_confirm_modal(sender: int, app_data: object) -> None:
    dpg.configure_item("save_confirm_modal", show=False)


def perform_save(sender: int, app_data: object) -> None:
    if not state.manager.path:
        append_log("No file loaded. Load a file before saving changes.")
        _hide_save_confirm_modal(sender, app_data)
        return

    new_text = state.manager.rebuild_text(state.blocks)
    try:
        state.manager.save(state.manager.path, new_text)
    except (OSError, ValueError) as exc:
        append_log(f"Unable to save changes: {exc}")
        _hide_save_confirm_modal(sender, app_data)
        return

    append_log(f"Changes saved to '{state.manager.path}'. Backup created with .bak extension.")
    state.original_blocks = list(state.blocks)
    state.modified_indices.clear()
    if dpg.does_item_exist("search_input"):
        update_question_list(dpg.get_value("search_input") or "")
    append_log("Cleared change markers; on-disk file now matches the UI.")
    _hide_save_confirm_modal(sender, app_data)


def _quit() -> None:
    # The exit callback also fires on the last frame; the flag keeps it from re-prompting.
    state.closing = True
    dpg.stop_dearpygui()


def on_close_requested() -> None:
    """Called when the window's close button is pressed (the viewport is created with disable_close)."""

    if state.closing:
        return
    if not state.modified_indices:
        _quit()
        return
    dpg.set_value(
        "close_confirm_text",
        f"You have {len(state.modified_indices)} unsaved change(s). Closing now discards them.",
    )
    dpg.configure_item("close_confirm_modal", show=True)


def on_close_cancel(sender: int, app_data: object) -> None:
    dpg.configure_item("close_confirm_modal", show=False)


def on_close_discard(sender: int, app_data: object) -> None:
    dpg.configure_item("close_confirm_modal", show=False)
    _quit()


def on_close_save(sender: int, app_data: object) -> None:
    dpg.configure_item("close_confirm_modal", show=False)
    perform_save(sender, app_data)
    if state.modified_indices:  # the save failed; perform_save logged why
        append_log("Not closing: the changes could not be saved.")
        return
    _quit()


def on_file_selected(file_path: Union[str, Path]) -> None:
    resolved_path = Path(file_path).expanduser().resolve()
    if not resolved_path.exists():
        append_log(f"Selected file does not exist: {resolved_path}")
        return

    try:
        dpg.set_value("loaded_file_label", f"Loading: {resolved_path.name}")
        state.manager = NVRAMManager().load_file(resolved_path)
    except ValueError as exc:
        append_log(str(exc))
        dpg.configure_item("load_error_modal", show=True)
        dpg.set_value("load_error_text", f"Failed to import file:\n{exc}")
        dpg.set_value("loaded_file_label", "No file loaded (failed to parse)")
        state.blocks = []
        state.original_blocks = []
        state.modified_indices = set()
        update_question_list("")
        return

    state.blocks = list(state.manager.blocks)
    state.original_blocks = list(state.manager.blocks)
    state.modified_indices = set()
    state.selected_index = None
    state.selected_indices = []
    if dpg.does_item_exist("search_input"):
        dpg.set_value("search_input", "")
    block_count = len(state.blocks)
    if block_count:
        dpg.set_value("loaded_file_label", f"Loaded: {resolved_path.name} ({block_count} blocks)")
        append_log(f"Loaded {resolved_path}: {block_count} question(s).")
    else:
        dpg.set_value("loaded_file_label", f"Loaded: {resolved_path.name} (no questions detected)")
        append_log(
            f"ERROR: No Setup Question blocks detected in {resolved_path}. "
            "Confirm this is a valid AMISCE export."
        )
    update_question_list("")
    if state.blocks:
        first_index = 0
        state.selected_index = first_index
        state.selected_indices = [first_index]
        selectable_tag = f"question_item_{first_index}"
        if dpg.does_item_exist(selectable_tag):
            dpg.set_value(selectable_tag, True)
        on_question_selected(None, None, {"index": first_index, "tag": selectable_tag})


def _dialog_start_path() -> str:
    try:
        if state.manager.path:
            return str(Path(state.manager.path).expanduser().resolve().parent)
        return str(Path.home())
    except Exception:
        return str(Path.cwd())


def open_file_dialog() -> None:
    if state.modified_indices:
        show_message(
            "Unsaved changes",
            "Opening another file would discard your unsaved changes. Save them first.",
        )
        return
    root = Tk()
    root.withdraw()
    try:
        file_path = filedialog.askopenfilename(initialdir=_dialog_start_path())
    finally:
        root.destroy()

    if not file_path:
        append_log("File selection cancelled or invalid path returned.")
        return

    on_file_selected(file_path)


def _set_scewin_runner(directory: Path) -> bool:
    try:
        state.scewin_runner = SCEWINRunner(directory)
    except SCEWINError as exc:
        append_log(str(exc))
        return False
    if dpg.does_item_exist("scewin_dir_label"):
        dpg.set_value("scewin_dir_label", f"SCEWIN: {state.scewin_runner.scewin_dir}")
    append_log(f"Using SCEWIN from {state.scewin_runner.scewin_dir}.")
    return True


def detect_scewin_dir() -> None:
    found = guess_scewin_dir(default_scewin_candidates())
    if found is not None:
        _set_scewin_runner(found)


def on_choose_scewin_dir(sender: int, app_data: object) -> None:
    root = Tk()
    root.withdraw()
    try:
        chosen = filedialog.askdirectory(
            title="Select the folder containing SCEWIN_64.exe", initialdir=_dialog_start_path()
        )
    finally:
        root.destroy()
    if chosen:
        _set_scewin_runner(Path(chosen))


def _require_scewin() -> Optional[SCEWINRunner]:
    if state.scewin_runner is None:
        append_log(
            "No SCEWIN folder selected. Click 'Choose SCEWIN folder' and pick the folder with SCEWIN_64.exe "
            "(get it with SCEHUB's DL_SCEWIN.exe: https://github.com/ab3lkaizen/SCEHUB)."
        )
    return state.scewin_runner


def _default_script_path() -> Path:
    if state.manager.path:
        return Path(state.manager.path)
    if state.scewin_runner is not None:
        return state.scewin_runner.scewin_dir / DEFAULT_SCRIPT_NAME
    return Path.cwd() / DEFAULT_SCRIPT_NAME


def _report_scewin_result(result: SCEWINResult) -> None:
    lines = result.summary_lines()
    for line in lines:
        append_log(line)
    if not result.ok:
        show_message("SCEWIN reported a problem", "\n".join(lines))


def show_message(title: str, text: str) -> None:
    dpg.set_value("message_title", title)
    dpg.set_value("message_text", text)
    dpg.configure_item("message_modal", show=True)


def on_scewin_export(sender: int, app_data: object) -> None:
    runner = _require_scewin()
    if runner is None:
        return
    if state.modified_indices:
        show_message(
            "Unsaved changes",
            "Exporting would overwrite the editor's view of the file. Save or discard your changes first.",
        )
        return

    default_path = _default_script_path()
    root = Tk()
    root.withdraw()
    try:
        chosen = filedialog.asksaveasfilename(
            title="Export NVRAM settings to",
            initialdir=str(default_path.parent),
            initialfile=default_path.name,
            defaultextension=".txt",
            filetypes=[("NVRAM script", "*.txt"), ("All files", "*.*")],
        )
    finally:
        root.destroy()
    if not chosen:
        append_log("Export cancelled.")
        return

    script_path = Path(chosen)
    append_log(f"Running SCEWIN export to {script_path} ...")
    try:
        result = runner.export(script_path)
    except SCEWINError as exc:
        append_log(str(exc))
        show_message("Export failed", str(exc))
        return
    _report_scewin_result(result)
    if result.ok and script_path.is_file():
        on_file_selected(script_path)


def _open_scewin_confirm(action: str, script_path: Path) -> None:
    runner = state.scewin_runner
    assert runner is not None

    issues = runner.check_import(script_path)
    if issues:
        text = "Import blocked by preflight checks:\n- " + "\n- ".join(issues)
        append_log(text)
        show_message("Import blocked", text)
        return

    if action == "restore":
        lines = [
            f"Re-import the original export:\n{script_path}",
            "",
            "This puts every setting back to how it was when you first exported.",
        ]
        risky = False
    else:
        changes = runner.changes_since_export(script_path)
        lines = [f"Write this file to NVRAM:\n{script_path}", ""]
        if changes is None:
            lines.append("No restore point exists for this file, so the changes cannot be listed.")
            lines.append("Export from BIOS first so you have a way back.")
            risky = False
        elif not changes:
            lines.append("No settings differ from the restore point.")
            risky = False
        else:
            lines.append(f"{len(changes)} setting(s) differ from the restore point:")
            lines.extend(f"- {change.format()}" for change in changes[:25])
            if len(changes) > 25:
                lines.append(f"...and {len(changes) - 25} more.")
            risky = any(change.risk_keywords for change in changes)
    lines += ["", "Changes take effect after a reboot."]

    state.pending_scewin_action = action
    state.pending_scewin_path = script_path
    dpg.set_value("scewin_confirm_text", "\n".join(lines))
    dpg.set_value("scewin_risk_ack", False)
    dpg.configure_item("scewin_risk_ack", show=risky)
    dpg.configure_item("scewin_confirm_modal", show=True)


def on_scewin_import(sender: int, app_data: object) -> None:
    if _require_scewin() is None:
        return
    if not state.manager.path:
        append_log("Load or export an NVRAM file before importing.")
        return
    if state.modified_indices:
        show_message("Unsaved changes", "Save your changes before importing; SCEWIN reads the file on disk.")
        return
    _open_scewin_confirm("import", Path(state.manager.path))


def on_scewin_restore(sender: int, app_data: object) -> None:
    if _require_scewin() is None:
        return
    restore_point = restore_point_path(_default_script_path().resolve())
    if not restore_point.is_file():
        show_message("No restore point", f"No restore point at:\n{restore_point}\nIt is created by the first export.")
        return
    _open_scewin_confirm("restore", restore_point)


def on_scewin_cancel(sender: int, app_data: object) -> None:
    dpg.configure_item("scewin_confirm_modal", show=False)
    state.pending_scewin_action = None
    state.pending_scewin_path = None
    append_log("SCEWIN import cancelled; NVRAM was not touched.")


def on_scewin_confirm(sender: int, app_data: object) -> None:
    runner = state.scewin_runner
    action = state.pending_scewin_action
    script_path = state.pending_scewin_path
    if runner is None or action is None or script_path is None:
        dpg.configure_item("scewin_confirm_modal", show=False)
        return
    if dpg.get_item_configuration("scewin_risk_ack").get("show") and not dpg.get_value("scewin_risk_ack"):
        append_log("Tick the risk acknowledgement before writing risky settings to NVRAM.")
        return

    dpg.configure_item("scewin_confirm_modal", show=False)
    state.pending_scewin_action = None
    state.pending_scewin_path = None
    append_log(f"Running SCEWIN {action} from {script_path} ...")
    try:
        result = replace(runner.import_script(script_path), action=action)
    except SCEWINError as exc:
        append_log(str(exc))
        show_message(f"{action.capitalize()} failed", str(exc))
        return
    _report_scewin_result(result)
    if result.ok:
        append_log("Reboot for the new NVRAM settings to take effect.")


def apply_dark_theme() -> None:
    with dpg.theme() as dark_theme:
        with dpg.theme_component(dpg.mvAll):
            dpg.add_theme_color(dpg.mvThemeCol_WindowBg, (32, 34, 42, 255))
            dpg.add_theme_color(dpg.mvThemeCol_ChildBg, (24, 26, 34, 255))
            dpg.add_theme_color(dpg.mvThemeCol_FrameBg, (44, 46, 54, 255))
            dpg.add_theme_color(dpg.mvThemeCol_TitleBgActive, (64, 68, 80, 255))
            dpg.add_theme_color(dpg.mvThemeCol_Button, (52, 56, 66, 255))
            dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (72, 76, 88, 255))
            dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (92, 96, 110, 255))
            dpg.add_theme_color(dpg.mvThemeCol_Text, (230, 230, 235, 255))
            dpg.add_theme_style(dpg.mvStyleVar_WindowRounding, 6)
            dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, 6)
            dpg.add_theme_style(dpg.mvStyleVar_FramePadding, 12, 9)
            dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing, 10, 10)
        with dpg.theme_component(dpg.mvRadioButton):
            dpg.add_theme_style(dpg.mvStyleVar_FramePadding, 12, 9)
            dpg.add_theme_color(dpg.mvThemeCol_FrameBg, (46, 52, 62, 255))
        with dpg.theme_component(dpg.mvInputText):
            dpg.add_theme_style(dpg.mvStyleVar_FramePadding, 12, 9)
            dpg.add_theme_color(dpg.mvThemeCol_FrameBgActive, (70, 76, 90, 255))
    dpg.bind_theme(dark_theme)


def load_fonts() -> None:
    with dpg.font_registry():
        fonts_dir = Path(resource_path("fonts"))
        regular_font = fonts_dir / "DejaVuSans.ttf"
        bold_font = fonts_dir / "DejaVuSans-Bold.ttf"
        mono_font = fonts_dir / "DejaVuSansMono.ttf"

        if not regular_font.exists():
            regular_font = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
        if not bold_font.exists():
            bold_font = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
        if not mono_font.exists():
            mono_font = Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf")

        if regular_font.exists():
            state.body_font = dpg.add_font(str(regular_font), 16)
        if bold_font.exists():
            state.header_font = dpg.add_font(str(bold_font), 22)
        if mono_font.exists():
            state.mono_font = dpg.add_font(str(mono_font), 14)

    if state.body_font:
        dpg.bind_font(state.body_font)


def build_ui() -> None:
    dpg.create_context()
    load_fonts()
    apply_dark_theme()
    layout = _calculate_layout_metrics()

    with dpg.window(tag="primary_window", label="NVRAM Tweaker - Dear PyGui", width=1200, height=820):
        dpg.add_spacer(height=8)

        with dpg.group(horizontal=True, horizontal_spacing=8):
            with dpg.child_window(tag="sidebar", width=layout["sidebar_width"], autosize_y=True, border=True):
                dpg.add_text("Setup Questions")
                dpg.add_text("No file loaded", tag="loaded_file_label", color=(180, 180, 200))
                dpg.add_button(label="Open NVRAM file", callback=lambda: open_file_dialog())
                with dpg.collapsing_header(label="SCEWIN: read / write BIOS", default_open=True):
                    dpg.add_text("SCEWIN: not selected", tag="scewin_dir_label", color=(180, 180, 200), wrap=300)
                    dpg.add_button(label="Choose SCEWIN folder", callback=on_choose_scewin_dir)
                    with dpg.group(horizontal=True):
                        dpg.add_button(label="Export from BIOS", callback=on_scewin_export)
                        dpg.add_button(label="Import to BIOS", callback=on_scewin_import)
                    dpg.add_button(label="Restore original", tag="scewin_restore_button", callback=on_scewin_restore)
                    with dpg.tooltip(parent="scewin_restore_button"):
                        dpg.add_text("Re-imports the untouched copy saved by your first export.")
                dpg.add_input_text(
                    tag="search_input",
                    label="Search",
                    hint="Type to filter questions...",
                    callback=on_search,
                )
                dpg.add_button(
                    tag="select_all_filtered_button",
                    label="Select All Filtered",
                    callback=on_select_all_filtered,
                )
                with dpg.tooltip(parent="select_all_filtered_button"):
                    dpg.add_text("Selects every question that matches the current search filter.")
                dpg.add_button(label="Clear Selection", callback=on_clear_selection)
                dpg.add_text(
                    "Batch mode active: applying changes to multiple questions.",
                    tag="batch_alert_label",
                    color=(255, 200, 120),
                    show=False,
                    wrap=300,
                )
                dpg.add_child_window(
                    tag="question_list_container",
                    autosize_x=True,
                    height=layout["sidebar_list_height"],
                    horizontal_scrollbar=True,
                    border=True,
                )

            with dpg.child_window(tag="editor_panel", autosize_x=True, autosize_y=True, border=True):
                with dpg.child_window(tag="editor_panel_inner", autosize_x=True, autosize_y=True, border=False):
                    dpg.add_spacer(height=10)
                    if state.header_font:
                        dpg.add_text("Question", tag="question_name", wrap=layout["wrap_width"], indent=6)
                        dpg.bind_item_font("question_name", state.header_font)
                    else:
                        dpg.add_text("Question", tag="question_name", wrap=layout["wrap_width"], indent=6)

                    dpg.add_text("Token: None", tag="question_token", color=(180, 180, 200), indent=6, wrap=layout["wrap_width"])
                    bind_mono_font("question_token")
                    dpg.add_separator()
                    dpg.add_text("Help String (read-only)", indent=6)
                    dpg.add_input_text(
                        tag="help_text",
                        multiline=True,
                        readonly=True,
                        height=110,
                        width=layout["wrap_width"],
                        indent=6,
                    )
                    dpg.add_separator()
                    dpg.add_text("Controls", indent=6)
                    dpg.add_child_window(
                        tag="control_region",
                        autosize_x=True,
                        border=False,
                        horizontal_scrollbar=True,
                    )
                    dpg.add_separator()
                    dpg.add_button(label="Save changes", callback=on_save, indent=6)

        with dpg.child_window(tag="console_panel", height=layout["console_panel_height"], autosize_x=True, border=True):
            dpg.add_text("Console")
            if state.mono_font:
                dpg.add_input_text(
                    tag="log_output",
                    multiline=True,
                    readonly=True,
                    height=-1,
                    width=-1,
                )
                dpg.bind_item_font("log_output", state.mono_font)
            else:
                dpg.add_input_text(
                    tag="log_output",
                    multiline=True,
                    readonly=True,
                    height=-1,
                    width=-1,
                )

    with dpg.window(tag="save_confirm_modal", modal=True, show=False, no_move=True, no_close=True, no_resize=True):
        dpg.add_text("Confirm Save")
        dpg.add_separator()
        dpg.add_text("", tag="save_confirm_text", wrap=420)
        with dpg.group(horizontal=True):
            dpg.add_button(label="Cancel", callback=_hide_save_confirm_modal)
            dpg.add_button(label="Save Changes", callback=perform_save)

    with dpg.window(tag="load_error_modal", modal=True, show=False, no_move=True, no_close=True, no_resize=True):
        dpg.add_text("Import failed")
        dpg.add_separator()
        dpg.add_text("", tag="load_error_text", wrap=480)
        dpg.add_button(label="Close", callback=lambda sender, app_data: dpg.configure_item("load_error_modal", show=False))

    with dpg.window(tag="risk_confirm_modal", modal=True, show=False, no_move=True, no_close=True, no_resize=True):
        dpg.add_text("Confirm risky change", color=(255, 170, 120))
        dpg.add_separator()
        dpg.add_text("", tag="risk_confirm_text", wrap=520)
        with dpg.group(horizontal=True):
            dpg.add_button(label="Cancel", callback=on_risk_cancel)
            dpg.add_button(label="Apply anyway", callback=on_risk_confirm)

    with dpg.window(tag="scewin_confirm_modal", modal=True, show=False, no_move=True, no_close=True, no_resize=True):
        dpg.add_text("Write settings to NVRAM", color=(255, 170, 120))
        dpg.add_separator()
        dpg.add_text("", tag="scewin_confirm_text", wrap=620)
        dpg.add_checkbox(
            tag="scewin_risk_ack",
            label="I understand some of these settings can stop the machine from booting, and I can clear CMOS.",
            show=False,
        )
        with dpg.group(horizontal=True):
            dpg.add_button(label="Cancel", callback=on_scewin_cancel)
            dpg.add_button(label="Write to NVRAM", callback=on_scewin_confirm)

    with dpg.window(tag="message_modal", modal=True, show=False, no_move=True, no_resize=True):
        dpg.add_text("", tag="message_title")
        dpg.add_separator()
        dpg.add_text("", tag="message_text", wrap=560)
        dpg.add_button(label="Close", callback=lambda sender, app_data: dpg.configure_item("message_modal", show=False))

    with dpg.window(tag="close_confirm_modal", modal=True, show=False, no_move=True, no_close=True, no_resize=True):
        dpg.add_text("Unsaved changes", color=(255, 170, 120))
        dpg.add_separator()
        dpg.add_text("", tag="close_confirm_text", wrap=420)
        with dpg.group(horizontal=True):
            dpg.add_button(label="Cancel", callback=on_close_cancel)
            dpg.add_button(label="Discard and close", callback=on_close_discard)
            dpg.add_button(label="Save and close", callback=on_close_save)

    dpg.create_viewport(title="NVRAM Tweaker UI", width=1200, height=840, resizable=True, disable_close=True)
    dpg.set_exit_callback(on_close_requested)
    dpg.setup_dearpygui()
    dpg.show_viewport()
    update_detail_wraps()
    if hasattr(dpg, "set_viewport_resize_callback"):
        dpg.set_viewport_resize_callback(on_viewport_resize)
    dpg.set_primary_window("primary_window", True)
    append_log("Ready. Load an NVRAM file, or export one from the BIOS with SCEWIN.")
    detect_scewin_dir()
    dpg.start_dearpygui()
    dpg.destroy_context()


if __name__ == "__main__":
    build_ui()
