"""
rate_guard.py — cross-session rate limit + auth error coordination.

Два режима блокировки:
  - rate_limit: provider заблочен на retry_after_s, fallback на другой provider
  - auth_error: provider заблочен на 1 час, fallback + предупреждение

Bridge проверяет ПЕРЕД запуском runner'а:
  - is_blocked("claude") → True? → сразу fallback, не тратить время на ошибку
  - get_reason("claude") → "rate_limit" / "auth" / None

Shared state: {AIOS_ROOT}/bridge/.rate_limits.json
Atomic write: tempfile + os.replace.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path

from path_config import rate_guard_file

log = logging.getLogger("bridge.rate_guard")

RATE_FILE = rate_guard_file()


def _next_half_hour() -> tuple[float, str]:
    """Timestamp и HH:MM ближайших :00 или :30 по МСК."""
    from datetime import datetime, timedelta, timezone
    MSK = timezone(timedelta(hours=3))
    now = datetime.now(MSK)
    minute = now.minute
    if minute < 30:
        target = now.replace(minute=30, second=0, microsecond=0)
    else:
        target = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    return target.timestamp(), target.strftime("%H:%M")


class RateGuard:
    def is_blocked(self, provider: str) -> bool:
        """True если provider сейчас заблокирован (rate limit или auth error)."""
        state = self._read()
        entry = state.get(provider)
        if not entry:
            return False
        return time.time() < entry.get("blocked_until", 0)

    def get_reason(self, provider: str) -> str | None:
        """Причина блокировки: 'rate_limit' / 'auth' / 'billing' / None."""
        state = self._read()
        entry = state.get(provider)
        if not entry:
            return None
        if time.time() >= entry.get("blocked_until", 0):
            return None
        return entry.get("reason", "rate_limit")

    def remaining_s(self, provider: str) -> float:
        """Сколько секунд до разблокировки. 0 = не заблокирован."""
        state = self._read()
        entry = state.get(provider)
        if not entry:
            return 0.0
        remaining = entry.get("blocked_until", 0) - time.time()
        return max(0.0, remaining)

    def should_wait(self, provider: str) -> float:
        """Legacy compat. Seconds to wait. 0 = go ahead."""
        return self.remaining_s(provider)

    def record_limit(self, provider: str, reason: str = "rate_limit") -> None:
        """Block provider until next :00 or :30 МСК."""
        state = self._read()
        now = time.time()
        blocked_until, until_str = _next_half_hour()
        state[provider] = {
            "blocked_until": blocked_until,
            "recorded_at": now,
            "until_str": until_str,
            "reason": reason,
            "pid": os.getpid(),
        }
        self._write(state)
        secs = int(blocked_until - now)
        log.warning(
            "[rate_guard] %s: %s → blocked until %s МСК (%ds, PID %d)",
            provider, reason, until_str, secs, os.getpid(),
        )

    def record_auth_error(self, provider: str) -> None:
        """Auth/billing error — блокируем до ближайших :00/:30."""
        self.record_limit(provider, reason="auth")

    def get_until_str(self, provider: str) -> str:
        """HH:MM МСК разблокировки, или ''."""
        state = self._read()
        entry = state.get(provider)
        if not entry or time.time() >= entry.get("blocked_until", 0):
            return ""
        return entry.get("until_str", "")

    def clear(self, provider: str) -> None:
        state = self._read()
        if provider in state:
            del state[provider]
            self._write(state)

    def _read(self) -> dict:
        if not RATE_FILE.exists():
            return {}
        try:
            data = json.loads(RATE_FILE.read_text())
            now = time.time()
            return {
                k: v for k, v in data.items()
                if v.get("blocked_until", 0) > now
            }
        except Exception:
            return {}

    def _write(self, state: dict) -> None:
        try:
            RATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(RATE_FILE.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(state, f)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, str(RATE_FILE))
            except Exception:
                os.unlink(tmp)
                raise
        except Exception:
            log.exception("[rate_guard] failed to write state")


_guard = RateGuard()

def is_blocked(provider: str) -> bool:
    return _guard.is_blocked(provider)

def get_reason(provider: str) -> str | None:
    return _guard.get_reason(provider)

def remaining_s(provider: str) -> float:
    return _guard.remaining_s(provider)

def should_wait(provider: str) -> float:
    return _guard.should_wait(provider)

def record_limit(provider: str, reason: str = "rate_limit") -> None:
    _guard.record_limit(provider, reason)

def record_auth_error(provider: str) -> None:
    _guard.record_auth_error(provider)

def get_until_str(provider: str) -> str:
    return _guard.get_until_str(provider)

def clear(provider: str) -> None:
    _guard.clear(provider)
