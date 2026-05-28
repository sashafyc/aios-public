"""
Тесты state machine session_manager.py.

Проверяют ключевые инварианты раздела 4 migration MD:
  - incoming_message переводит в ACTIVE
  - WAITING_FOR отключает idle compact
  - RESULT закрывает WAITING и возвращает ACTIVE
  - needs_compact() True только для ACTIVE без активности
  - can_daily_reset() False если есть открытые WAITING
  - state переживает save/load (.state файл)
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))

from session_manager import SessionManager, State, SessionState


MSK = timezone(timedelta(hours=3))


def _sm(tmp_path: Path) -> SessionManager:
    workdirs = {"tasks": tmp_path / "tasks", "research": tmp_path / "research"}
    for p in workdirs.values():
        p.mkdir(parents=True, exist_ok=True)
    return SessionManager(workdirs)


def test_initial_state_is_idle(tmp_path):
    sm = _sm(tmp_path)
    st = sm.load("tasks")
    assert st.state == State.IDLE
    assert st.turns == 0


def test_incoming_message_activates(tmp_path):
    sm = _sm(tmp_path)
    st = sm.on_incoming_message("tasks")
    assert st.state == State.ACTIVE
    assert st.turns == 1
    assert st.last_activity is not None


def test_response_without_waiting_stays_active(tmp_path):
    sm = _sm(tmp_path)
    sm.on_incoming_message("tasks")
    st = sm.on_response("tasks")
    assert st.state == State.ACTIVE
    assert st.waiting_for == []


def test_response_with_waiting_goes_to_waiting(tmp_path):
    sm = _sm(tmp_path)
    sm.on_incoming_message("tasks")
    st = sm.on_response("tasks", waiting_for=["research:base-x"])
    assert st.state == State.WAITING
    assert st.waiting_for == ["research:base-x"]
    assert st.waiting_since is not None


def test_result_resolves_waiting(tmp_path):
    sm = _sm(tmp_path)
    sm.on_incoming_message("tasks")
    sm.on_response("tasks", waiting_for=["research:base-x", "scout:y"])
    st = sm.on_waiting_resolved("tasks", "research:base-x")
    assert st.state == State.WAITING  # ещё один open
    st = sm.on_waiting_resolved("tasks", "scout:y")
    assert st.state == State.ACTIVE   # все закрыты
    assert st.waiting_for == []


def test_needs_compact_only_in_active(tmp_path):
    sm = _sm(tmp_path)
    sm.on_incoming_message("tasks")
    sm.on_response("tasks")
    # сразу после ответа — не надо
    assert sm.needs_compact("tasks", idle_timeout_min=60) is False

    # подделаем last_activity на 61 минуту назад
    st = sm.load("tasks")
    past = (datetime.now(MSK) - timedelta(minutes=61)).isoformat(timespec="seconds")
    st.last_activity = past
    sm.save(st)
    assert sm.needs_compact("tasks", idle_timeout_min=60) is True


def test_waiting_state_never_compacts(tmp_path):
    sm = _sm(tmp_path)
    sm.on_incoming_message("tasks")
    sm.on_response("tasks", waiting_for=["research:x"])
    st = sm.load("tasks")
    past = (datetime.now(MSK) - timedelta(hours=5)).isoformat(timespec="seconds")
    st.last_activity = past
    sm.save(st)
    # критично: WAITING не компактится никогда
    assert sm.needs_compact("tasks", idle_timeout_min=60) is False


def test_can_daily_reset_false_while_waiting(tmp_path):
    sm = _sm(tmp_path)
    sm.on_incoming_message("tasks")
    sm.on_response("tasks", waiting_for=["research:x"])
    assert sm.can_daily_reset("tasks") is False


def test_can_daily_reset_true_when_idle(tmp_path):
    sm = _sm(tmp_path)
    assert sm.can_daily_reset("tasks") is True


def test_can_daily_reset_true_when_active_no_waiting(tmp_path):
    sm = _sm(tmp_path)
    sm.on_incoming_message("tasks")
    sm.on_response("tasks")
    assert sm.can_daily_reset("tasks") is True


def test_waiting_stale_escalation(tmp_path):
    sm = _sm(tmp_path)
    sm.on_incoming_message("tasks")
    sm.on_response("tasks", waiting_for=["research:x"])
    st = sm.load("tasks")
    st.waiting_since = (datetime.now(MSK) - timedelta(hours=25)).isoformat(timespec="seconds")
    sm.save(st)
    assert sm.waiting_stale("tasks", max_hours=24) is True


def test_state_survives_reload(tmp_path):
    sm1 = _sm(tmp_path)
    sm1.on_incoming_message("tasks")
    sm1.on_response("tasks", waiting_for=["research:x"])
    # "рестарт bridge" — создаём новый SessionManager с той же директорией
    sm2 = SessionManager({"tasks": tmp_path / "tasks"})
    st = sm2.load("tasks")
    assert st.state == State.WAITING
    assert st.waiting_for == ["research:x"]


def test_reset_to_idle_clears_everything(tmp_path):
    sm = _sm(tmp_path)
    sm.on_incoming_message("tasks")
    sm.on_response("tasks", waiting_for=["research:x"])
    sm.on_waiting_resolved("tasks", "research:x")
    sm.on_reset_to_idle("tasks")
    st = sm.load("tasks")
    assert st.state == State.IDLE
    assert st.turns == 0
    assert st.waiting_for == []
    assert st.last_reset is not None
