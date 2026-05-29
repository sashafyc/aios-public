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


def test_send_disabled_by_default(monkeypatch):
    monkeypatch.delenv("AIOS_BUGREPORT", raising=False)
    monkeypatch.delenv("AIOS_BUGREPORT_URL", raising=False)
    rep = bug_report.build_report(source="error", description="x")
    assert bug_report.send(rep) is False, "по умолчанию отправка должна быть выключена"


def test_send_disabled_without_url(monkeypatch):
    monkeypatch.setenv("AIOS_BUGREPORT", "1")
    monkeypatch.setenv("AIOS_BUGREPORT_URL", "")
    rep = bug_report.build_report(source="error", description="x")
    assert bug_report.send(rep) is False, "без URL отправка невозможна даже при =1"
