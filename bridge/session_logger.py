"""
session_logger.py — usage/cost tracking (Уровень 3, раздел 9 migration MD).

На каждый запуск агента → одна строка JSONL с tokens_in/out/cache_read/cache_write,
стоимостью и длительностью. Используется для:
  - контроля расхода лимитов runner'ов
  - анализа стоимости работы агентов
  - отлова деградации cache hit ratio

Файлы: {AIOS_ROOT}/logs/sessions/YYYY-MM-DD.jsonl

Входы: log_session(agent, result)
Используют: tg_bridge после каждого run().
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from path_config import log_dir

log = logging.getLogger("bridge.session_log")

MSK = timezone(timedelta(hours=3))
SESSIONS_DIR = log_dir("sessions")
_lock = threading.Lock()


def log_session(
    agent: str,
    *,
    session_id: str | None,
    model: str,
    usage: dict[str, Any],
    cost_usd: float,
    duration_ms: int,
    error: str | None = None,
    **extra: Any,
) -> None:
    entry = {
        "ts": datetime.now(MSK).isoformat(timespec="seconds"),
        "agent": agent,
        "session_id": session_id,
        "model": model,
        "tokens_in": usage.get("input_tokens") or usage.get("tokens_in"),
        "tokens_out": usage.get("output_tokens") or usage.get("tokens_out"),
        "cache_read": usage.get("cache_read_input_tokens") or usage.get("cache_read"),
        "cache_write": usage.get("cache_creation_input_tokens") or usage.get("cache_write"),
        "cost_usd": cost_usd,
        "duration_ms": duration_ms,
        "error": error,
        **extra,
    }
    path = SESSIONS_DIR / f"{datetime.now(MSK).strftime('%Y-%m-%d')}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with _lock:
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        log.exception("failed to write session log")
