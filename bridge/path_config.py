"""
path_config.py — пути системы, всё строится от AIOS_ROOT.

AIOS_ROOT задаётся:
  1. переменной окружения AIOS_ROOT (install.sh прописывает в .env), либо
  2. ключом [global] aios_root в agents.toml, либо
  3. по умолчанию — родительская папка bridge/ (корень установки).

Никаких абсолютных путей в коде — всё относительно корня установки.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path


def _default_root() -> Path:
    """Корень установки = родитель папки bridge/ (где лежит этот файл)."""
    return Path(__file__).resolve().parent.parent


def _root_from_toml() -> Path | None:
    toml_path = Path(__file__).resolve().parent / "agents.toml"
    if not toml_path.exists():
        return None
    try:
        raw = tomllib.loads(toml_path.read_text(encoding="utf-8"))
        val = raw.get("global", {}).get("aios_root")
        return Path(val) if val else None
    except Exception:
        return None


def aios_root() -> Path:
    env = os.getenv("AIOS_ROOT")
    if env:
        return Path(env)
    toml_root = _root_from_toml()
    if toml_root:
        return toml_root
    return _default_root()


def log_root() -> Path:
    return aios_root() / "logs"


def log_dir(name: str) -> Path:
    return log_root() / name


def bridge_log_dir() -> Path:
    return log_dir("bridge")


def bridge_runtime_dir() -> Path:
    return aios_root() / "bridge"


def queue_dir() -> Path:
    return bridge_runtime_dir() / "queue"


def inbox_dir() -> Path:
    return aios_root() / "workspace" / "temp" / "inbox"


def rate_guard_file() -> Path:
    return bridge_runtime_dir() / ".rate_limits.json"


def agent_state_path(agent: str, legacy_workdir: Path) -> Path:
    return legacy_workdir / ".state"
