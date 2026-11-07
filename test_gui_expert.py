"""GUI side of expert mode (headless, same approach as test_gui_smoke.py)."""
from __future__ import annotations

import pytest

dpg = pytest.importorskip("dearpygui.dearpygui")

import nvram_settings  # noqa: E402
import scewin_runner  # noqa: E402
from test_gui_smoke import gui, select, shown  # noqa: E402,F401  (gui is a fixture)
from test_scewin_integration import FakeRun  # noqa: E402


def _exported_sample(gui, tmp_path, monkeypatch):
    scewin_dir = tmp_path / "SCEWIN"
    scewin_dir.mkdir()
    for name in scewin_runner.REQUIRED_FILES:
        (scewin_dir / name).write_bytes(b"")
    script = tmp_path / "nvram.txt"
    run = FakeRun()
    gui.state.scewin_runner = scewin_runner.SCEWINRunner(scewin_dir, run=run, admin_check=lambda: True)
    monkeypatch.setattr(gui.filedialog, "asksaveasfilename", lambda **kw: str(script))
    gui.on_scewin_export(None, None)
    return script, run


def _choose_disabled_xhci(gui) -> None:
    idx = select(gui, "XHCI Hand-off")
    mapping = dpg.get_item_user_data("option_selector")["map"]
    disabled = next(label for label in mapping if "Disabled" in label)
    gui.on_option_change("option_selector", disabled, {"block_index": idx, "map": mapping})


def _enable_expert(gui) -> None:
    gui.on_expert_toggle("expert_mode_checkbox", True)
    dpg.set_value("expert_ack", True)
    gui.on_expert_confirm(None, None)


def test_expert_checkbox_starts_off_and_the_title_is_plain(gui) -> None:
    assert dpg.get_value("expert_mode_checkbox") is False
    assert "EXPERT" not in dpg.get_item_label("primary_window")


def test_turning_it_on_asks_for_the_acknowledgement(gui) -> None:
    gui.on_expert_toggle("expert_mode_checkbox", True)
    assert shown("expert_confirm_modal")
    assert dpg.get_value("expert_mode_checkbox") is False  # not ticked until acknowledged

    gui.on_expert_confirm(None, None)  # acknowledgement box left empty
    assert not nvram_settings.expert_mode() and shown("expert_confirm_modal")

    dpg.set_value("expert_ack", True)
    gui.on_expert_confirm(None, None)
    assert nvram_settings.expert_mode() and not shown("expert_confirm_modal")
    assert dpg.get_value("expert_mode_checkbox") is True
    assert "EXPERT MODE" in dpg.get_item_label("primary_window")


def test_cancelling_leaves_expert_mode_off(gui) -> None:
    gui.on_expert_toggle("expert_mode_checkbox", True)
    gui.on_expert_cancel(None, None)
    assert not nvram_settings.expert_mode() and not shown("expert_confirm_modal")
    assert dpg.get_value("expert_mode_checkbox") is False


def test_turning_it_off_needs_no_questions(gui) -> None:
    _enable_expert(gui)
    gui.on_expert_toggle("expert_mode_checkbox", False)
    assert not nvram_settings.expert_mode() and not shown("expert_confirm_modal")
    assert "EXPERT" not in dpg.get_item_label("primary_window")


def test_a_risky_edit_asks_normally_without_expert_mode(gui, tmp_path, monkeypatch) -> None:
    _exported_sample(gui, tmp_path, monkeypatch)
    _choose_disabled_xhci(gui)
    assert shown("risk_confirm_modal")


def test_a_risky_edit_applies_straight_away_in_expert_mode(gui, tmp_path, monkeypatch) -> None:
    _exported_sample(gui, tmp_path, monkeypatch)
    _enable_expert(gui)
    _choose_disabled_xhci(gui)
    assert not shown("risk_confirm_modal")
    assert gui.state.modified_indices
    assert any("Expert mode: applied without a risk prompt" in m for m in gui.state.log_messages)


def test_import_drops_the_tick_box_but_still_lists_the_risk(gui, tmp_path, monkeypatch) -> None:
    script, run = _exported_sample(gui, tmp_path, monkeypatch)
    _enable_expert(gui)
    _choose_disabled_xhci(gui)
    gui.perform_save(None, None)

    gui.on_scewin_import(None, None)

    assert shown("scewin_confirm_modal") and not shown("scewin_risk_ack")
    assert "[RISK: XHCI]" in dpg.get_value("scewin_confirm_text")  # still visible in the change list
    gui.on_scewin_confirm(None, None)  # no tick needed, but the user still pressed "Write to NVRAM"
    assert run.calls[-1][1:] == ["/i", "/s", str(script.resolve())]


def test_import_still_needs_the_tick_without_expert_mode(gui, tmp_path, monkeypatch) -> None:
    _, run = _exported_sample(gui, tmp_path, monkeypatch)
    _choose_disabled_xhci(gui)
    gui.on_risk_confirm(None, None)
    gui.perform_save(None, None)
    gui.on_scewin_import(None, None)
    assert shown("scewin_risk_ack")
    calls = len(run.calls)
    gui.on_scewin_confirm(None, None)
    assert len(run.calls) == calls
