"""Headless tests of the Dear PyGui front end.

The real viewport is never opened: window-level calls are stubbed, while the widgets,
callbacks and state are real, so the tests drive the same handlers a click would.
Skipped when ``dearpygui`` is not installed.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import List

import pytest

dpg = pytest.importorskip("dearpygui.dearpygui")

import nvram_gui  # noqa: E402
import scewin_runner  # noqa: E402
from test_scewin_integration import FakeRun  # noqa: E402

FIXTURE = Path(__file__).parent / "tests" / "fixtures" / "real_export_trimmed.txt"


class _NoTk:
    def withdraw(self) -> None:
        pass

    def destroy(self) -> None:
        pass


@pytest.fixture
def gui(monkeypatch, tmp_path):
    """Build the real UI with window-level calls stubbed; yields the module."""

    stopped: List[bool] = []
    real_destroy = dpg.destroy_context
    for name in (
        "create_viewport",
        "setup_dearpygui",
        "show_viewport",
        "set_viewport_resize_callback",
        "set_viewport_title",
        "set_primary_window",
        "set_exit_callback",
        "start_dearpygui",
        "destroy_context",
    ):
        monkeypatch.setattr(dpg, name, lambda *a, **k: None)
    monkeypatch.setattr(dpg, "stop_dearpygui", lambda: stopped.append(True))
    monkeypatch.setattr(nvram_gui, "state", nvram_gui.GUIState())
    monkeypatch.setattr(nvram_gui, "Tk", _NoTk)
    monkeypatch.setattr(nvram_gui, "_get_viewport_dimensions", lambda: (1200, 840))

    nvram_gui.build_ui()
    nvram_gui.stopped = stopped  # type: ignore[attr-defined]
    yield nvram_gui
    real_destroy()


def shown(tag: str) -> bool:
    return bool(dpg.get_item_configuration(tag)["show"])


def select(gui, name: str) -> int:
    idx = next(i for i, b in enumerate(gui.state.blocks) if b.name == name and not b.is_commented)
    gui.on_question_selected(None, None, {"index": idx, "tag": f"question_item_{idx}"})
    return idx


def load(gui, source: Path, tmp_path: Path) -> Path:
    target = tmp_path / "nvram.txt"
    shutil.copyfile(source, target)
    gui.on_file_selected(target)
    return target


# --- loading ---------------------------------------------------------------


def test_loading_logs_a_single_line_and_lists_only_active_questions(gui, tmp_path) -> None:
    load(gui, FIXTURE, tmp_path)
    loaded = [m for m in gui.state.log_messages if m.startswith("Loaded ")]
    assert len(loaded) == 1 and "19 question(s)" in loaded[0]
    listed = set(gui.state.label_to_index.values())
    assert listed and all(not gui.state.blocks[i].is_commented for i in listed)
    assert len(listed) == 18  # 19 questions, one of them commented out


def test_loading_a_non_export_logs_one_error(gui, tmp_path) -> None:
    empty = tmp_path / "empty.txt"
    empty.write_text("nothing here\n")
    gui.on_file_selected(empty)
    errors = [m for m in gui.state.log_messages if m.startswith(("ERROR", "CRITICAL"))]
    assert len(errors) == 1 and errors[0].startswith("ERROR:")


# --- numeric limits --------------------------------------------------------


def test_one_byte_value_shows_its_range_and_rejects_256(gui, tmp_path) -> None:
    load(gui, FIXTURE, tmp_path)
    idx = select(gui, "SataRedrv Com Option1 Address")  # bare "Value =0", Width=01
    assert "0 to 255" in dpg.get_value("value_range_hint")
    assert dpg.get_item_configuration("value_input_int")["max_value"] == 255

    dpg.set_value("value_input_int", 300)
    gui.on_value_change(None, None, {"block_index": idx, "input_tag": "value_input_int"})
    assert not gui.state.modified_indices
    assert any("out of range" in m for m in gui.state.log_messages)

    dpg.set_value("value_input_int", 200)
    gui.on_value_change(None, None, {"block_index": idx, "input_tag": "value_input_int"})
    assert idx in gui.state.modified_indices


def test_four_byte_value_uses_the_text_field(gui, tmp_path) -> None:
    load(gui, FIXTURE, tmp_path)
    select(gui, "Current VDDIO Value (mV)")  # Width=04 does not fit a 32-bit signed widget
    assert dpg.does_item_exist("value_input_text") and not dpg.does_item_exist("value_input_int")
    assert "0 to 4294967295" in dpg.get_value("value_range_hint")


# --- risk confirmation -----------------------------------------------------


def test_confirm_tier_change_waits_for_confirmation(gui, tmp_path) -> None:
    load(gui, FIXTURE, tmp_path)
    idx = select(gui, "XHCI Controller0 enable")
    mapping = dpg.get_item_user_data("option_selector")["map"]
    other = next(label for label in mapping if not label.startswith("*"))

    gui.on_option_change("option_selector", other, {"block_index": idx, "map": mapping})
    assert shown("risk_confirm_modal") and not gui.state.modified_indices

    gui.on_risk_cancel(None, None)
    assert not shown("risk_confirm_modal") and not gui.state.modified_indices

    gui.on_option_change("option_selector", other, {"block_index": idx, "map": mapping})
    gui.on_risk_confirm(None, None)
    assert idx in gui.state.modified_indices


def test_note_tier_change_applies_without_a_prompt(gui, tmp_path) -> None:
    load(gui, FIXTURE, tmp_path)
    idx = select(gui, "ASPM Mode Control")  # note tier: marked in the diff, no modal
    mapping = dpg.get_item_user_data("option_selector")["map"]
    other = next(label for label in mapping if not label.startswith("*"))

    gui.on_option_change("option_selector", other, {"block_index": idx, "map": mapping})

    assert not shown("risk_confirm_modal") and idx in gui.state.modified_indices


# --- closing and opening with unsaved changes --------------------------------


def _make_dirty(gui, tmp_path) -> Path:
    path = load(gui, FIXTURE, tmp_path)
    idx = select(gui, "SataRedrv Com Option1 Address")
    dpg.set_value("value_input_int", 9)
    gui.on_value_change(None, None, {"block_index": idx, "input_tag": "value_input_int"})
    assert gui.state.modified_indices
    return path


def test_closing_with_nothing_unsaved_quits_at_once(gui) -> None:
    gui.on_close_requested()
    assert gui.stopped == [True] and not shown("close_confirm_modal")


def test_closing_with_unsaved_changes_asks_first(gui, tmp_path) -> None:
    _make_dirty(gui, tmp_path)
    gui.on_close_requested()
    assert shown("close_confirm_modal") and gui.stopped == []
    assert "1 unsaved change" in dpg.get_value("close_confirm_text")

    gui.on_close_cancel(None, None)
    assert not shown("close_confirm_modal") and gui.stopped == []


def test_discard_and_close_quits_without_writing(gui, tmp_path) -> None:
    path = _make_dirty(gui, tmp_path)
    before = path.read_bytes()
    gui.on_close_requested()
    gui.on_close_discard(None, None)
    assert gui.stopped == [True] and path.read_bytes() == before
    gui.on_close_requested()  # the exit callback also fires on the last frame
    assert gui.stopped == [True] and not shown("close_confirm_modal")


def test_save_and_close_writes_then_quits(gui, tmp_path) -> None:
    path = _make_dirty(gui, tmp_path)
    before = path.read_bytes()
    gui.on_close_requested()
    gui.on_close_save(None, None)
    assert gui.stopped == [True]
    assert path.read_bytes() != before and (tmp_path / "nvram.txt.bak").read_bytes() == before


def test_save_and_close_stays_open_when_saving_fails(gui, tmp_path, monkeypatch) -> None:
    _make_dirty(gui, tmp_path)

    def broken_save(*args, **kwargs):
        raise ValueError("disk full")

    monkeypatch.setattr(gui.state.manager, "save", broken_save)
    gui.on_close_requested()
    gui.on_close_save(None, None)
    assert gui.stopped == [] and gui.state.modified_indices
    assert any("Not closing" in m for m in gui.state.log_messages)


def test_opening_another_file_with_unsaved_changes_is_refused(gui, tmp_path) -> None:
    _make_dirty(gui, tmp_path)
    gui.open_file_dialog()
    assert shown("message_modal") and "Save them first" in dpg.get_value("message_text")


# --- SCEWIN export -> edit -> import -> restore ------------------------------


def test_export_edit_import_restore_flow(gui, tmp_path, monkeypatch) -> None:
    scewin_dir = tmp_path / "SCEWIN"
    scewin_dir.mkdir()
    for name in scewin_runner.REQUIRED_FILES:
        (scewin_dir / name).write_bytes(b"")
    script = tmp_path / "nvram.txt"
    run = FakeRun()
    gui.state.scewin_runner = scewin_runner.SCEWINRunner(scewin_dir, run=run, admin_check=lambda: True)
    monkeypatch.setattr(gui.filedialog, "asksaveasfilename", lambda **kw: str(script))

    gui.on_scewin_export(None, None)
    assert run.calls[0][1] == "/o" and (tmp_path / "nvram.original.txt").is_file()
    assert len(gui.state.blocks) == 3

    idx = select(gui, "XHCI Hand-off")
    mapping = dpg.get_item_user_data("option_selector")["map"]
    disabled = next(label for label in mapping if "Disabled" in label)
    gui.on_option_change("option_selector", disabled, {"block_index": idx, "map": mapping})
    gui.on_risk_confirm(None, None)

    gui.on_scewin_import(None, None)  # refused: unsaved changes
    assert shown("message_modal")
    dpg.configure_item("message_modal", show=False)

    gui.perform_save(None, None)
    gui.on_scewin_import(None, None)
    assert shown("scewin_confirm_modal") and "XHCI Hand-off" in dpg.get_value("scewin_confirm_text")
    assert shown("scewin_risk_ack")

    calls = len(run.calls)
    gui.on_scewin_confirm(None, None)  # acknowledgement not ticked
    assert len(run.calls) == calls
    dpg.set_value("scewin_risk_ack", True)
    gui.on_scewin_confirm(None, None)
    assert run.calls[-1][1:] == ["/i", "/s", str(script.resolve())]

    gui.on_scewin_restore(None, None)
    gui.on_scewin_confirm(None, None)
    assert run.calls[-1][1:] == ["/i", "/s", str((tmp_path / "nvram.original.txt").resolve())]
    assert any("SCEWIN restore succeeded" in m for m in gui.state.log_messages)


def test_window_title_shows_the_version(gui) -> None:
    from nvram_version import __version__

    assert dpg.get_item_label("primary_window") == f"NVRAM Tweaker {__version__}"
