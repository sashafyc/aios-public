"""
conversation_log.py — master conversation log.

Единый JSONL-журнал всех взаимодействий: юзер ↔ агент, агент ↔ агент,
делегации, аппрувалы, ошибки. Append-only, одна строка = одно событие.
Ротация по дням: {AIOS_ROOT}/logs/conversations/YYYY-MM-DD.jsonl
+ отдельный лог только делегаций: {AIOS_ROOT}/logs/delegations/YYYY-MM-DD.jsonl

Зачем: откопать "что говорилось 3 недели назад" через jq.

Входы: log_event(type, **kwargs) и log_delegation(...)
Выходы: файл JSONL
Используют: tg_bridge в каждой точке обмена сообщениями.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from path_config import log_root

log = logging.getLogger("bridge.conv_log")

MSK = timezone(timedelta(hours=3))

LOG_ROOT = log_root()
CONVERSATIONS_DIR = LOG_ROOT / "conversations"
DELEGATIONS_DIR = LOG_ROOT / "delegations"

_write_lock = threading.Lock()


def _today() -> str:
    return datetime.now(MSK).strftime("%Y-%m-%d")


def _now() -> str:
    return datetime.now(MSK).isoformat(timespec="seconds")


def _append_jsonl(path: Path, entry: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry, ensure_ascii=False)
    with _write_lock:
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


def log_event(event_type: str, **kwargs: Any) -> None:
    """
    event_type: user_msg | agent_msg | delegation | result |
                approval | error | system
    kwargs: произвольные поля (from, to, topic, text, task, usage, ...)
    """
    entry = {"ts": _now(), "type": event_type, **kwargs}
    path = CONVERSATIONS_DIR / f"{_today()}.jsonl"
    try:
        _append_jsonl(path, entry)
    except Exception:
        log.exception("failed to write conversation log")


def log_delegation(
    from_agent: str,
    to_agent: str,
    task_id: str | None,
    instructions: str,
    **extra: Any,
) -> None:
    """Отдельный сжатый лог только межагентных делегаций — удобно для analytics."""
    entry = {
        "ts": _now(),
        "from": from_agent,
        "to": to_agent,
        "task_id": task_id,
        "instructions": instructions,
        **extra,
    }
    path = DELEGATIONS_DIR / f"{_today()}.jsonl"
    try:
        _append_jsonl(path, entry)
    except Exception:
        log.exception("failed to write delegation log")
    # дублируем в общий conversation log для единой ленты
    log_event("delegation", **entry)
