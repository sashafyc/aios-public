#!/usr/bin/env python3
"""
doctor.py — диагностика здоровья bridge.

Проверяет: bot tokens, GROUP_CHAT_ID, опциональные API keys, systemd (Linux),
диск, lockfile, логи, очередь, конфигурацию агентов.
Запуск: python3 {AIOS_ROOT}/bridge/doctor.py
Результат: список check'ов с ok/warning/error (exit 1 если есть errors).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent))

# Load .env if exists
env_file = Path(__file__).parent / ".env"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

import agents_registry
from path_config import bridge_log_dir


@dataclass
class Check:
    name: str
    status: str  # "ok", "warning", "error"
    detail: str

    def __str__(self):
        icons = {"ok": "✅", "warning": "⚠️", "error": "❌"}
        return f"{icons.get(self.status, '?')} {self.name}: {self.detail}"


async def check_bot_token(token_env: str) -> Check:
    token = os.environ.get(token_env, "")
    if not token:
        return Check(token_env, "error", "not set")
    try:
        import aiohttp
        tg_base = os.environ.get("TG_API_BASE", "https://api.telegram.org")
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{tg_base}/bot{token}/getMe", timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    bot_name = data.get("result", {}).get("username", "?")
                    return Check(token_env, "ok", f"@{bot_name}")
                return Check(token_env, "error", f"HTTP {resp.status}")
    except Exception as e:
        return Check(token_env, "error", str(e)[:80])


def check_env_var(name: str) -> Check:
    val = os.environ.get(name, "")
    if not val:
        return Check(name, "error", "not set")
    return Check(name, "ok", f"set ({len(val)} chars)")


def check_optional_env_var(name: str, feature: str) -> Check:
    """Опциональный ключ — отсутствие = warning (фича выключена), не error."""
    val = os.environ.get(name, "")
    if not val:
        return Check(name, "warning", f"not set — {feature} выключено")
    return Check(name, "ok", f"set ({len(val)} chars)")


def check_group_chat() -> Check:
    val = os.environ.get("AIOS_GROUP_CHAT_ID", "")
    if not val:
        # может быть задан в agents.toml [global]
        gid = getattr(agents_registry, "GROUP_CHAT_ID", 0)
        if gid:
            return Check("GROUP_CHAT_ID", "ok", f"{gid} (из agents.toml)")
        return Check("GROUP_CHAT_ID", "error", "не задан — бот не знает свою группу")
    return Check("GROUP_CHAT_ID", "ok", str(val))


def check_systemd() -> Check:
    # На macOS нет systemd — пропускаем (мост запускается через launchd/nohup)
    if sys.platform == "darwin":
        return Check("systemd", "ok", "skipped (macOS — нет systemd)")
    if not shutil.which("systemctl"):
        return Check("systemd", "ok", "skipped (нет systemctl)")
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "aios-bridge"],
            capture_output=True, text=True, timeout=5,
        )
        status = result.stdout.strip()
        if status == "active":
            return Check("systemd aios-bridge", "ok", "active")
        return Check("systemd aios-bridge", "warning", f"{status} (мост может работать вручную)")
    except Exception as e:
        return Check("systemd aios-bridge", "warning", f"check failed: {e}")


def check_disk() -> Check:
    usage = shutil.disk_usage("/")
    free_gb = usage.free / (1024**3)
    total_gb = usage.total / (1024**3)
    pct_used = (usage.used / usage.total) * 100
    if pct_used > 95:
        return Check("disk", "error", f"{free_gb:.1f}GB free ({pct_used:.0f}% used)")
    if pct_used > 85:
        return Check("disk", "warning", f"{free_gb:.1f}GB free ({pct_used:.0f}% used)")
    return Check("disk", "ok", f"{free_gb:.1f}GB free ({pct_used:.0f}% used)")


def check_lockfile() -> Check:
    lock_path = Path(__file__).parent / ".bridge.lock"
    if not lock_path.exists():
        return Check("lockfile", "warning", "no lockfile (bridge not running?)")
    try:
        data = json.loads(lock_path.read_text())
        pid = data.get("pid", 0)
        try:
            os.kill(pid, 0)
            return Check("lockfile", "ok", f"PID {pid} alive")
        except OSError:
            return Check("lockfile", "warning", f"PID {pid} dead (stale lockfile)")
    except Exception:
        return Check("lockfile", "error", "corrupt lockfile")


def _bridge_started_at() -> datetime | None:
    try:
        result = subprocess.run(
            ["systemctl", "show", "aios-bridge", "-p", "ExecMainStartTimestamp", "--value"],
            capture_output=True, text=True, timeout=5,
        )
        raw = result.stdout.strip()
        if not raw or raw == "n/a":
            return None
        return datetime.strptime(raw, "%a %Y-%m-%d %H:%M:%S %Z").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def check_logs() -> Check:
    log_file = bridge_log_dir() / "errors.log"
    if not log_file.exists():
        return Check("errors.log", "ok", "no errors.log (clean)")
    size = log_file.stat().st_size
    if size > 10 * 1024 * 1024:
        return Check("errors.log", "warning", f"{size // 1024 // 1024}MB (consider rotation)")

    started_at = _bridge_started_at()
    if started_at and log_file.stat().st_mtime < started_at.timestamp():
        return Check("errors.log", "ok", f"{size // 1024}KB, no errors since bridge start")

    lines = log_file.read_text(encoding="utf-8", errors="replace").strip().splitlines()
    recent = [l for l in lines[-50:] if "ERROR" in l]
    if recent:
        return Check("errors.log", "warning", f"{len(recent)} errors in last 50 lines")
    return Check("errors.log", "ok", f"{size // 1024}KB, recent lines clean")


def check_queue() -> Check:
    queue_dir = Path(__file__).parent / "queue"
    if not queue_dir.exists():
        return Check("queue", "ok", "no queue dir")
    files = list(queue_dir.glob("*.json"))
    if not files:
        return Check("queue", "ok", "empty")
    # Check age of oldest file
    import time
    oldest = min(f.stat().st_mtime for f in files)
    age_min = (time.time() - oldest) / 60
    if age_min > 10:
        return Check("queue", "error", f"{len(files)} files, oldest {age_min:.0f}min (stuck?)")
    return Check("queue", "ok", f"{len(files)} files, oldest {age_min:.0f}min")


async def check_agents() -> list[Check]:
    checks = []
    for cfg in agents_registry.enabled_agents():
        issues = []
        if cfg.enabled_in_tg:
            if not cfg.topic_id and not cfg.topic_ids:
                issues.append("no topic_id")
            if not cfg.bot_token_env:
                issues.append("no bot_token_env")
            elif not os.environ.get(cfg.bot_token_env):
                issues.append(f"{cfg.bot_token_env} not set")
        if not cfg.workdir or not Path(cfg.workdir).exists():
            issues.append(f"workdir missing: {cfg.workdir}")

        if issues:
            checks.append(Check(f"agent:{cfg.name}", "warning", "; ".join(issues)))
        else:
            mode = f"topic={cfg.topic_id}" if cfg.enabled_in_tg else "queue-only"
            checks.append(Check(f"agent:{cfg.name}", "ok", f"{mode} runner={cfg.runner_type}"))
    return checks


async def run_all() -> list[Check]:
    checks: list[Check] = []

    # System checks
    checks.append(check_systemd())
    checks.append(check_disk())
    checks.append(check_lockfile())
    checks.append(check_logs())
    checks.append(check_queue())

    # Конфигурация TG
    checks.append(check_group_chat())

    # Опциональные API keys (отсутствие = фича выключена, не ошибка)
    checks.append(check_optional_env_var("ASSEMBLYAI_API_KEY", "голосовые/транскрибация"))
    checks.append(check_optional_env_var("DEEPSEEK_API_KEY", "runner deepseek"))
    checks.append(check_optional_env_var("OPENAI_API_KEY", "runner codex (API-режим)"))

    # Bot tokens (deduplicated)
    seen_tokens = set()
    for cfg in agents_registry.enabled_agents():
        if cfg.bot_token_env and cfg.bot_token_env not in seen_tokens:
            seen_tokens.add(cfg.bot_token_env)
            checks.append(await check_bot_token(cfg.bot_token_env))

    # Agents
    checks.extend(await check_agents())

    return checks


async def main():
    checks = await run_all()

    errors = [c for c in checks if c.status == "error"]
    warnings = [c for c in checks if c.status == "warning"]
    oks = [c for c in checks if c.status == "ok"]

    print(f"\n🏥 Bridge Doctor — {len(checks)} checks\n")
    if errors:
        print("─── ERRORS ───")
        for c in errors:
            print(f"  {c}")
    if warnings:
        print("─── WARNINGS ───")
        for c in warnings:
            print(f"  {c}")
    print(f"\n✅ {len(oks)} ok  ⚠️ {len(warnings)} warnings  ❌ {len(errors)} errors\n")

    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    asyncio.run(main())
