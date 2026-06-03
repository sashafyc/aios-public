"""
agents_registry.py — конфигурация всех агентов команды.

Загрузка из agents.toml с hot-reload (watchfiles).
API: get(), enabled_agents(), AGENTS dict, TOPIC_TO_AGENT.
"""

from __future__ import annotations

import logging
import os
import threading
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from path_config import aios_root

log = logging.getLogger("bridge.registry")

# agents.toml — пользовательский конфиг (его генерит install.sh, он gitignored,
# чтобы git pull при обновлении его НЕ трогал и не было конфликтов). В репозитории
# трекается только шаблон agents.toml.example — он же используется как fallback
# для тестов/CI/свежего клона, где реального agents.toml ещё нет.
_BRIDGE_DIR = Path(__file__).parent
_TOML_PATH = _BRIDGE_DIR / "agents.toml"
if not _TOML_PATH.exists():
    _TOML_PATH = _BRIDGE_DIR / "agents.toml.example"

# ───────────── defaults ─────────────
# AIOS_ROOT — корень установки (от path_config: env AIOS_ROOT / toml / автоопределение).
# GROUP_CHAT_ID — id TG-группы; задаётся в .env (AIOS_GROUP_CHAT_ID) или agents.toml.

AIOS_ROOT = aios_root()
GROUP_CHAT_ID = int(os.getenv("AIOS_GROUP_CHAT_ID", "0") or "0")


@dataclass(frozen=True)
class AgentConfig:
    name: str
    display_name: str
    topic_id: int
    bot_token_env: str
    workdir: Path
    model: str
    runner_type: str
    role: str
    enabled: bool = True
    autonomous: bool = False
    can_delegate_to: list[str] = field(default_factory=list)
    idle_timeout_min: Optional[int] = None
    stream: bool = False
    topic_ids: Optional[list[int]] = None
    enabled_in_tg: bool = True
    chat_id: Optional[int] = None
    timeout_s: int = 1800
    run_as_user: Optional[str] = None
    settings_path: Optional[str] = None
    extra_chat_ids: Optional[list[int]] = None


def _load_toml() -> dict[str, AgentConfig]:
    """Parse agents.toml → dict of AgentConfig."""
    global AIOS_ROOT, GROUP_CHAT_ID

    raw = tomllib.loads(_TOML_PATH.read_text(encoding="utf-8"))
    g = raw.get("global", {})

    # Пустая строка в toml = "автоопределение" (берём из path_config / env).
    root = Path(g.get("aios_root") or str(AIOS_ROOT))
    AIOS_ROOT = root
    # .env (AIOS_GROUP_CHAT_ID) имеет приоритет; иначе берём из toml
    GROUP_CHAT_ID = GROUP_CHAT_ID or g.get("group_chat_id", 0)

    agents: dict[str, AgentConfig] = {}
    for name, cfg in raw.get("agents", {}).items():
        workdir_rel = cfg.get("workdir", f"agents/{name}")
        workdir = root / workdir_rel if not workdir_rel.startswith("/") else Path(workdir_rel)

        # settings_path: относительный → резолвим от корня в абсолютный
        # (агент запускается с cwd=workdir, поэтому --settings нужен абсолютным)
        sp = cfg.get("settings_path")
        if sp and not sp.startswith("/"):
            sp = str(root / sp)

        agents[name] = AgentConfig(
            name=name,
            display_name=cfg.get("display_name", name),
            topic_id=cfg.get("topic_id", 0),
            bot_token_env=cfg.get("bot_token_env", ""),
            workdir=workdir,
            model=cfg.get("model", "claude-sonnet-4-6"),
            runner_type=cfg.get("runner_type", "claude"),
            role=cfg.get("role", ""),
            enabled=cfg.get("enabled", True),
            autonomous=cfg.get("autonomous", False),
            can_delegate_to=cfg.get("can_delegate_to", []),
            idle_timeout_min=cfg.get("idle_timeout_min"),
            stream=cfg.get("stream", False),
            topic_ids=cfg.get("topic_ids"),
            enabled_in_tg=cfg.get("enabled_in_tg", True),
            chat_id=cfg.get("chat_id"),
            timeout_s=cfg.get("timeout_s", 1800),
            run_as_user=cfg.get("run_as_user"),
            settings_path=sp,
            extra_chat_ids=cfg.get("extra_chat_ids"),
        )

    return agents


def _rebuild_topic_map(agents: dict[str, AgentConfig]) -> dict[int, str]:
    topic_map: dict[int, str] = {}
    for name, agent in agents.items():
        if agent.enabled and agent.chat_id is None and agent.topic_id not in topic_map:
            topic_map[agent.topic_id] = name
    return topic_map


# Initial load
AGENTS: dict[str, AgentConfig] = _load_toml()
TOPIC_TO_AGENT: dict[int, str] = _rebuild_topic_map(AGENTS)
_last_mtime: float = _TOML_PATH.stat().st_mtime if _TOML_PATH.exists() else 0
_reload_lock = threading.Lock()


def reload_if_changed() -> bool:
    """Check if agents.toml changed and reload. Returns True if reloaded."""
    global AGENTS, TOPIC_TO_AGENT, _last_mtime

    if not _TOML_PATH.exists():
        return False

    mtime = _TOML_PATH.stat().st_mtime
    if mtime <= _last_mtime:
        return False

    with _reload_lock:
        # Double-check after acquiring lock
        mtime = _TOML_PATH.stat().st_mtime
        if mtime <= _last_mtime:
            return False

        try:
            new_agents = _load_toml()
            old_names = set(AGENTS.keys())
            new_names = set(new_agents.keys())

            added = new_names - old_names
            removed = old_names - new_names
            changed = {
                n for n in old_names & new_names
                if AGENTS[n] != new_agents[n]
            }

            AGENTS = new_agents
            TOPIC_TO_AGENT = _rebuild_topic_map(AGENTS)
            _last_mtime = mtime

            if added or removed or changed:
                log.info(
                    "agents.toml reloaded: +%s -%s ~%s",
                    list(added) or "[]",
                    list(removed) or "[]",
                    list(changed) or "[]",
                )
            # авто-синхронизация карты раннеров из agents.toml
            # (knowledge/system/agents-runners.md). Источник правды — agents.toml.
            try:
                import subprocess as _sp, sys as _sys
                from pathlib import Path as _P
                _gen = _P(__file__).resolve().parent.parent / "scripts" / "gen-runners-doc.py"
                if _gen.exists():
                    _r = _sp.run([_sys.executable, str(_gen)],
                                 capture_output=True, text=True, timeout=30)
                    if _r.returncode != 0:
                        log.error("runner-doc regen failed: %s", (_r.stderr or "")[:400])
                    else:
                        log.info("runner-doc regen OK (agents.toml → agents-runners.md)")
            except Exception:
                log.exception("failed to trigger runner-doc regen")
            return True
        except Exception:
            log.exception("Failed to reload agents.toml")
            return False


# ───────────── API (backward-compatible) ─────────────

def get(name: str) -> AgentConfig:
    return AGENTS[name]


def by_topic(topic_id: int) -> Optional[AgentConfig]:
    name = TOPIC_TO_AGENT.get(topic_id)
    return AGENTS[name] if name else None


def by_chat_and_topic(chat_id: int, topic_id: int) -> Optional[AgentConfig]:
    for cfg in AGENTS.values():
        if not cfg.enabled:
            continue
        eff_chat = cfg.chat_id if cfg.chat_id else GROUP_CHAT_ID
        if eff_chat != chat_id:
            continue
        if cfg.topic_id == topic_id and cfg.topic_ids is None:
            return cfg
        if cfg.topic_ids is not None and topic_id in cfg.topic_ids:
            return cfg
    return None


def all_agents() -> list[AgentConfig]:
    return list(AGENTS.values())


def enabled_agents() -> list[AgentConfig]:
    return [a for a in AGENTS.values() if a.enabled]


def autonomous_agents() -> list[AgentConfig]:
    return [a for a in AGENTS.values() if a.autonomous and a.enabled]
