"""
test_product_config.py — проверка дефолтной конфигурации aios-public.

Гарантирует что agents.toml парсится и содержит 3 агента из коробки
с правильными runner'ами и делегацией.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))

import agents_registry as r  # noqa: E402


def test_three_default_agents():
    names = set(r.AGENTS.keys())
    assert names == {"assistant", "sysadmin", "scriber"}


def test_all_enabled():
    assert len(r.enabled_agents()) == 3


def test_one_bot_token():
    tokens = {a.bot_token_env for a in r.enabled_agents()}
    assert tokens == {"BOT_TOKEN"}


def test_assistant_delegates_to_scriber():
    assistant = r.get("assistant")
    assert "scriber" in assistant.can_delegate_to


def test_sysadmin_no_delegation():
    assert r.get("sysadmin").can_delegate_to == []


def test_all_claude_by_default():
    for a in r.enabled_agents():
        assert a.runner_type == "claude"


def test_workdirs_under_root():
    root = r.AIOS_ROOT
    for a in r.enabled_agents():
        assert str(a.workdir).startswith(str(root))
