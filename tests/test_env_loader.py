"""test_env_loader.py — загрузка .env (используется tg_bridge/doctor/trigger)."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))

from env_loader import load_env  # noqa: E402


def test_loads_values_and_strips_quotes(tmp_path):
    env = tmp_path / ".env"
    env.write_text('FOO_X="bar 123"\n# comment\nBAZ_X=qux\n\n', encoding="utf-8")
    os.environ.pop("FOO_X", None)
    os.environ.pop("BAZ_X", None)
    load_env(env)
    assert os.environ["FOO_X"] == "bar 123"   # кавычки сняты, пробел сохранён
    assert os.environ["BAZ_X"] == "qux"


def test_does_not_override_existing(tmp_path):
    env = tmp_path / ".env"
    env.write_text("PRESET_X=fromfile\n", encoding="utf-8")
    os.environ["PRESET_X"] = "fromenv"   # systemd-приоритет
    load_env(env)
    assert os.environ["PRESET_X"] == "fromenv"


def test_missing_file_is_noop(tmp_path):
    load_env(tmp_path / "nope.env")  # не должно падать
