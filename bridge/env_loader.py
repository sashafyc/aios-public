"""
env_loader.py — загрузка bridge/.env в окружение процесса.

Нужно потому, что:
  - systemd подхватывает .env через EnvironmentFile,
  - но ручной запуск (`python bridge/tg_bridge.py`) и macOS launchd — нет.
Поэтому tg_bridge / doctor / trigger вызывают load_env() при старте сами.

setdefault: уже заданные переменные окружения (например из systemd) имеют приоритет.
"""

from __future__ import annotations

import os
from pathlib import Path


def load_env(env_path: Path | str | None = None) -> None:
    """Прочитать bridge/.env и положить значения в os.environ (не перезаписывая существующие)."""
    if env_path is None:
        env_path = Path(__file__).resolve().parent / ".env"
    env_path = Path(env_path)
    if not env_path.exists():
        return
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key:
                os.environ.setdefault(key, val)
    except Exception:
        # тихо: отсутствие/битый .env не должен ронять старт (doctor покажет проблему)
        pass
