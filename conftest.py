"""Test-wide setup: never read or write the developer's real settings file."""
from __future__ import annotations

import pytest

import nvram_settings


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path_factory, monkeypatch):
    monkeypatch.setenv(nvram_settings.ENV_VAR, str(tmp_path_factory.mktemp("settings") / "settings.json"))
