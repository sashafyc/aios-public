"""
session_manager.py — state machine сессии агента.

4 состояния: IDLE / ACTIVE / WAITING / BUSY.
Хранение: .state файл в workdir агента (JSON). Пережимается при рестарте bridge.

Зачем: если агент в WAITING — idle timer отключён, /compact не срабатывает,
daily reset 06:30 не трогает сессию. Это ключевое правило раздела 4 migration MD:
WAITING может длиться хоть сутки — это легальное состояние, не зависание.

Входы: имя агента, события (incoming_message, response_sent, waiting_for, ...)
Выходы: текущее состояние + решение bridge что делать (compact / reset / пропустить)
Используют: tg_bridge, delegation_router, trigger.py.
"""

from __future__ import annotations

import enum
import json
import logging
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from path_config import agent_state_path


log = logging.getLogger("bridge.session")

MSK = timezone(timedelta(hours=3))


class State(str, enum.Enum):
    IDLE = "IDLE"          # Процесса нет, только context.md на диске.
    ACTIVE = "ACTIVE"      # Диалог с Сашей, сессия жива, idle timer работает.
    WAITING = "WAITING"    # Ждёт результат делегации / cron / внешнего события. Timer выключен.
    BUSY = "BUSY"          # Tool call прямо сейчас. Переходное состояние.


@dataclass
class SessionState:
    """In-memory представление .state файла агента."""

    agent: str
    state: State = State.IDLE
    session_id: Optional[str] = None           # session_id из Claude CLI resume
    last_activity: Optional[str] = None        # ISO-8601 МСК
    waiting_for: list[str] = field(default_factory=list)  # ["research:task-id", ...]
    waiting_since: Optional[str] = None
    waiting_reason: Optional[str] = None
    last_compact: Optional[str] = None
    last_reset: Optional[str] = None
    turns: int = 0                              # счётчик сообщений в текущей сессии
    real_turns_since_compact: int = 0             # реальных сообщений с последнего compact

    def to_json(self) -> dict:
        d = asdict(self)
        d["state"] = self.state.value
        return d

    @classmethod
    def from_json(cls, data: dict) -> "SessionState":
        data = dict(data)
        data["state"] = State(data.get("state", "IDLE"))
        # отфильтровать неизвестные поля на случай будущих миграций
        allowed = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in allowed})


class SessionManager:
    """
    Один экземпляр на bridge. Кэширует SessionState каждого агента в памяти,
    атомарно сбрасывает на диск при каждом изменении.
    """

    def __init__(self, agents_workdirs: dict[str, Path]):
        self._workdirs = agents_workdirs
        self._cache: dict[str, SessionState] = {}

    # ──────── IO ────────

    def _state_path(self, agent: str) -> Path:
        return agent_state_path(agent, self._workdirs[agent])

    def load(self, agent: str) -> SessionState:
        """Прочитать состояние с диска или вернуть IDLE-дефолт."""
        if agent in self._cache:
            return self._cache[agent]

        path = self._state_path(agent)
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                st = SessionState.from_json(data)
            except Exception as exc:  # битый файл не должен валить bridge
                log.warning("state file corrupt for %s: %s — resetting", agent, exc)
                st = SessionState(agent=agent)
        else:
            st = SessionState(agent=agent)

        self._cache[agent] = st
        return st

    def save(self, st: SessionState) -> None:
        """Атомарная запись .state с fsync."""
        path = self._state_path(st.agent)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".state.tmp")
        data = json.dumps(st.to_json(), ensure_ascii=False, indent=2)
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        self._cache[st.agent] = st

    # ──────── переходы ────────

    def on_incoming_message(self, agent: str) -> SessionState:
        """
        Пришло сообщение агенту (от Саши или от другого агента).
        IDLE/WAITING/ACTIVE → ACTIVE. Обновляем last_activity.
        """
        st = self.load(agent)
        st.state = State.ACTIVE
        st.last_activity = self._now()
        st.turns += 1
        st.real_turns_since_compact += 1
        self.save(st)
        return st

    def on_response(
        self,
        agent: str,
        waiting_for: Optional[list[str]] = None,
        waiting_reason: Optional[str] = None,
    ) -> SessionState:
        """
        Агент отдал ответ.
        Если ответ содержит [WAITING_FOR:...] → state=WAITING (idle timer off).
        Иначе → ACTIVE (idle timer продолжает тикать).
        """
        st = self.load(agent)
        st.last_activity = self._now()
        if waiting_for:
            st.state = State.WAITING
            st.waiting_for = list(waiting_for)
            st.waiting_since = self._now()
            st.waiting_reason = waiting_reason
        else:
            st.state = State.ACTIVE
            st.waiting_for = []
            st.waiting_since = None
            st.waiting_reason = None
        self.save(st)
        return st

    def on_waiting_resolved(self, agent: str, waiting_key: str) -> SessionState:
        """
        Пришёл [RESULT:…] который закрывает одну из ожидаемых делегаций.
        Если больше нет открытых WAITING — переводим в ACTIVE.
        """
        st = self.load(agent)
        if waiting_key in st.waiting_for:
            st.waiting_for.remove(waiting_key)
        if not st.waiting_for:
            st.state = State.ACTIVE
            st.waiting_since = None
            st.waiting_reason = None
        st.last_activity = self._now()
        self.save(st)
        return st

    def on_busy(self, agent: str, busy: bool) -> SessionState:
        """
        Переключение BUSY (идёт tool call). Возврат — в ACTIVE (если нет WAITING).
        """
        st = self.load(agent)
        if busy:
            st.state = State.BUSY
        else:
            st.state = State.WAITING if st.waiting_for else State.ACTIVE
        self.save(st)
        return st

    def on_compact(self, agent: str) -> None:
        st = self.load(agent)
        st.last_compact = self._now()
        st.real_turns_since_compact = 0
        self.save(st)

    def on_reset_to_idle(self, agent: str) -> None:
        """Daily reset: /new → IDLE. Сохраняем только last_reset."""
        st = self.load(agent)
        st.state = State.IDLE
        st.session_id = None
        st.turns = 0
        st.real_turns_since_compact = 0
        st.waiting_for = []
        st.waiting_since = None
        st.waiting_reason = None
        st.last_reset = self._now()
        self.save(st)

    # ──────── запросы ────────

    def needs_compact(self, agent: str, idle_timeout_min: int = 60) -> bool:
        """
        Вернуть True если агент в ACTIVE и сидит без активности >= idle_timeout_min.
        WAITING и BUSY никогда не компактятся.
        """
        st = self.load(agent)
        if st.state != State.ACTIVE:
            return False
        if not st.last_activity:
            return False
        # Compact только если были реальные сообщения с последнего compact.
        # Без этого: idle 60min → compact → idle 60sec → compact → ∞ loop.
        if st.real_turns_since_compact <= 0:
            return False
        age = datetime.now(MSK) - self._parse(st.last_activity)
        return age >= timedelta(minutes=idle_timeout_min)

    def can_daily_reset(self, agent: str) -> bool:
        """
        Daily reset допустим если state ∈ {ACTIVE, IDLE} и нет открытых WAITING.
        BUSY — пропускаем (подождём окончания tool call). WAITING — не трогаем.
        """
        st = self.load(agent)
        if st.waiting_for:
            return False
        return st.state in (State.ACTIVE, State.IDLE)

    def waiting_stale(self, agent: str, max_hours: int = 24) -> bool:
        """True если WAITING >24ч — пора эскалировать Саше 'зависло'."""
        st = self.load(agent)
        if st.state != State.WAITING or not st.waiting_since:
            return False
        age = datetime.now(MSK) - self._parse(st.waiting_since)
        return age >= timedelta(hours=max_hours)

    def set_session_id(self, agent: str, session_id: Optional[str]) -> None:
        """Сохранить claude session_id для будущего --resume."""
        st = self.load(agent)
        if st.session_id == session_id:
            return
        st.session_id = session_id
        self.save(st)

    def get_session_id(self, agent: str) -> Optional[str]:
        """Вернуть сохранённый session_id или None."""
        return self.load(agent).session_id

    # ──────── utils ────────

    @staticmethod
    def _now() -> str:
        return datetime.now(MSK).isoformat(timespec="seconds")

    @staticmethod
    def _parse(ts: str) -> datetime:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=MSK)
        return dt
