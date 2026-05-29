"""Тесты редакции и сборки баг-репортов (приватность)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))

import bug_report


def test_scrub_masks_secrets():
    key = "sk-ant-api03-" + "A" * 90
    out = bug_report.scrub(f"failed with key {key}")
    assert key not in out, "API-ключ должен быть замаскирован"


def test_scrub_strips_user_paths():
    out = bug_report.scrub("error at /home/john/clients/acme/file.py and /Users/jane/x")
    assert "/home/john" not in out and "/Users/jane" not in out
    assert "~" in out


def test_scrub_limits_length():
    out = bug_report.scrub("x" * 10000)
    assert len(out) <= bug_report.MAX_FIELD + 32


def test_build_report_has_no_pii_fields():
    rep = bug_report.build_report(source="error", description="boom", error="trace",
                                  agent="assistant", runner="claude", error_kind="UNKNOWN")
    # только ожидаемые поля, ничего лишнего (имя юзера, переписка и т.п.)
    assert set(rep) == {"ts", "install_id", "version", "os", "python",
                        "source", "agent", "runner", "error_kind", "description", "error"}
    assert rep["source"] == "error" and rep["runner"] == "claude"


def test_send_off_without_url(monkeypatch):
    # дефолт ВКЛ (флаг не задан), но без URL отправки быть не может
    monkeypatch.delenv("AIOS_BUGREPORT", raising=False)
    monkeypatch.delenv("AIOS_BUGREPORT_URL", raising=False)
    rep = bug_report.build_report(source="error", description="x")
    assert bug_report.send(rep) is False, "без URL отправка невозможна"


def test_send_off_when_disabled(monkeypatch):
    # явное выключение AIOS_BUGREPORT=0 даже при заданном URL
    monkeypatch.setenv("AIOS_BUGREPORT", "0")
    monkeypatch.setenv("AIOS_BUGREPORT_URL", "https://example.test/bugreport")
    rep = bug_report.build_report(source="error", description="x")
    assert bug_report.send(rep) is False, "AIOS_BUGREPORT=0 → отправки нет"
