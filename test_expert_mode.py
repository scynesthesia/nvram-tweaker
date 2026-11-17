"""Expert mode: off by default, needs an acknowledgement, and only removes the *interactive*
extra confirmations. Scripts, the integrity checks and the final write question are unchanged."""
from __future__ import annotations

import functools
import json
from pathlib import Path
from typing import List

import pytest

import nvram_cli_prompts
import nvram_settings
import nvram_tweaker_cli as cli
import scewin_runner
from test_scewin_integration import SAMPLE_EXPORT, FakeRun

RISKY = ["XHCI Hand-off (Token: 1A): [01]Enabled -> [00]Disabled  [RISK: XHCI]"]


def run_cli(capsys, *argv, interactive=False, answers=()):
    replies = iter(answers)
    code = cli.main([str(a) for a in argv], interactive=interactive, ask=lambda prompt: next(replies))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# --- the stored setting -----------------------------------------------------------------


def test_expert_mode_is_off_by_default() -> None:
    assert nvram_settings.expert_mode() is False


@pytest.mark.parametrize("content", ["not json", "[]", '"true"', '{"expert_mode": "yes"}', '{"expert_mode": 1}'])
def test_anything_but_a_real_true_means_off(content: str) -> None:
    path = nvram_settings.settings_path()
    path.write_text(content, encoding="utf-8")
    assert nvram_settings.expert_mode() is False


def test_turning_it_on_needs_the_acknowledgement() -> None:
    with pytest.raises(ValueError, match="acknowledgement"):
        nvram_settings.set_expert_mode(True)
    assert nvram_settings.expert_mode() is False


def test_on_and_off_are_stored_and_the_acknowledgement_is_dated() -> None:
    nvram_settings.set_expert_mode(True, acknowledged=True)
    stored = json.loads(nvram_settings.settings_path().read_text(encoding="utf-8"))
    assert nvram_settings.expert_mode() is True and stored["expert_acknowledged_at"]
    nvram_settings.set_expert_mode(False)
    assert nvram_settings.expert_mode() is False
    assert [p.name for p in nvram_settings.settings_path().parent.iterdir()] == ["settings.json"]  # no temp file left


# --- confirm_write ---------------------------------------------------------------------


def _confirm(**kwargs):
    said: List[str] = []
    asked: List[str] = []
    answers = iter(kwargs.pop("answers", []))

    def ask(prompt: str) -> str:
        asked.append(prompt)
        return next(answers)

    ok = nvram_cli_prompts.confirm_write("Go?", ask=ask, say=said.append, **kwargs)
    return ok, said, asked


def test_expert_mode_skips_the_typed_yes_for_a_person_at_a_terminal() -> None:
    nvram_settings.set_expert_mode(True, acknowledged=True)
    ok, said, asked = _confirm(risky_lines=RISKY, yes=True, interactive=True)
    assert ok and asked == []
    assert any("RISK" in line for line in said) and any("Expert mode is on" in line for line in said)


def test_expert_mode_still_asks_the_final_question() -> None:
    nvram_settings.set_expert_mode(True, acknowledged=True)
    ok, _, asked = _confirm(risky_lines=RISKY, yes=False, interactive=True, answers=["y"])
    assert ok and len(asked) == 1 and "(Y/N)" in asked[0]
    ok, _, _ = _confirm(risky_lines=RISKY, yes=False, interactive=True, answers=["n"])
    assert not ok


def test_expert_mode_never_widens_what_a_script_may_write() -> None:
    nvram_settings.set_expert_mode(True, acknowledged=True)
    ok, said, _ = _confirm(risky_lines=RISKY, yes=True, interactive=False)
    assert not ok and any("--allow-risky" in line for line in said)


def test_without_expert_mode_the_typed_yes_is_still_required() -> None:
    ok, _, asked = _confirm(risky_lines=RISKY, yes=True, interactive=True, answers=["nope"])
    assert not ok and len(asked) == 1


# --- config command ---------------------------------------------------------------------


def test_config_show(capsys) -> None:
    code, out, _ = run_cli(capsys, "config", "show")
    assert code == 0 and "expert mode: off" in out and "settings.json" in out


def test_a_script_cannot_turn_expert_mode_on_without_saying_so(capsys) -> None:
    code, _, err = run_cli(capsys, "config", "set", "expert", "true")
    assert code == 1 and "--i-understand" in err and not nvram_settings.expert_mode()


def test_i_understand_flag_turns_it_on_and_false_turns_it_off(capsys) -> None:
    assert run_cli(capsys, "config", "set", "expert", "true", "--i-understand")[0] == 0
    assert nvram_settings.expert_mode()
    assert run_cli(capsys, "config", "set", "expert", "false")[0] == 0
    assert not nvram_settings.expert_mode()


def test_interactive_enable_needs_the_typed_phrase(capsys) -> None:
    code, _, _ = run_cli(capsys, "config", "set", "expert", "true", interactive=True, answers=["yes"])
    assert code == 1 and not nvram_settings.expert_mode()
    code, _, err = run_cli(capsys, "config", "set", "expert", "true", interactive=True, answers=["i understand"])
    assert code == 0 and nvram_settings.expert_mode() and "brick" in err


def test_set_prints_a_banner_while_expert_mode_is_on(capsys, tmp_path: Path) -> None:
    script = tmp_path / "nvram.txt"
    script.write_text(SAMPLE_EXPORT, encoding="utf-8")
    nvram_settings.set_expert_mode(True, acknowledged=True)
    code, _, err = run_cli(capsys, "set", script, "XHCI Hand-off", "Disabled", "--dry-run")
    assert code == 0 and "expert mode is on" in err
    nvram_settings.set_expert_mode(False)
    _, _, err = run_cli(capsys, "set", script, "XHCI Hand-off", "Disabled", "--dry-run")
    assert "expert mode" not in err


def test_interactive_set_of_a_risky_setting_in_expert_mode(capsys, tmp_path: Path) -> None:
    script = tmp_path / "nvram.txt"
    script.write_text(SAMPLE_EXPORT, encoding="utf-8")
    nvram_settings.set_expert_mode(True, acknowledged=True)
    # Only the final Y/N is answered: a typed YES would raise StopIteration if it were asked for.
    code, out, _ = run_cli(capsys, "set", script, "XHCI Hand-off", "Disabled", interactive=True, answers=["y"])
    assert code == 0 and "[RISK: XHCI]" in out
    assert "*[00]Disabled" in script.read_text(encoding="utf-8")


def test_import_integrity_checks_still_apply_in_expert_mode(capsys, tmp_path: Path, monkeypatch) -> None:
    scewin_dir = tmp_path / "SCEWIN"
    scewin_dir.mkdir()
    for name in scewin_runner.REQUIRED_FILES:
        (scewin_dir / name).write_bytes(b"")
    fake = FakeRun()
    monkeypatch.setattr(
        scewin_runner, "SCEWINRunner", functools.partial(scewin_runner.SCEWINRunner, run=fake, admin_check=lambda: True)
    )
    nvram_settings.set_expert_mode(True, acknowledged=True)
    script = tmp_path / "nvram.txt"
    run_cli(capsys, "export", "--scewin", scewin_dir, "--file", script)
    script.write_text(script.read_text(encoding="utf-8").replace("Offset\t=12", "Offset\t=FF"), encoding="utf-8")

    code, _, err = run_cli(capsys, "import", "--scewin", scewin_dir, "--file", script, "--yes", interactive=True)

    assert code == 1 and "differ in more than the selected option" in err
    assert all(call[1] != "/i" for call in fake.calls)
