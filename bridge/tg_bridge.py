"""
tg_bridge.py — multi-agent Telegram оркестратор (aios-public).

Архитектура:
  * Один или несколько TG-ботов; агенты группируются по bot_token.
  * Каждый агент слушает свой топик (message_thread_id) в общей группе.
  * Бот реагирует только в своей группе (GROUP_CHAT_ID); любой участник группы
    может писать в любой топик.
  * Отправка ответов агента идёт его же ботом — в TG видно кто отвечает.
  * Межагентная доставка ([DELEGATE:X]) — через runners + отправку ботом X.
  * idle compact, WAITING эскалация — в фоновом maintenance_loop.

Запуск:
    source {AIOS_ROOT}/bridge/.env && python3 tg_bridge.py

Конфигурация: {AIOS_ROOT}/bridge/.env (см. .env.example).
"""

from __future__ import annotations

import asyncio
import atexit
import hashlib
import re
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Optional

BRIDGE_VERSION = "1.0.2"  # aios-public

sys.path.insert(0, str(Path(__file__).parent))

# Грузим .env ДО чтения переменных окружения (нужно для ручного запуска и macOS
# launchd, где нет systemd EnvironmentFile). systemd-переменные имеют приоритет.
from env_loader import load_env
load_env()

# Таймзона и время ежедневного сброса сессий — конфигурируется через .env.
# TIMEZONE_OFFSET_HOURS — смещение от UTC (по умолчанию +3, МСК).
# DAILY_RESET_HOUR / DAILY_RESET_MINUTE — когда сбрасывать сессии агентов.
_TZ_OFFSET = int(os.getenv("TIMEZONE_OFFSET_HOURS", "3"))
LOCAL_TZ = timezone(timedelta(hours=_TZ_OFFSET))
MSK = LOCAL_TZ  # обратная совместимость с остальным кодом
DAILY_RESET_HOUR = int(os.getenv("DAILY_RESET_HOUR", "6"))
DAILY_RESET_MINUTE = int(os.getenv("DAILY_RESET_MINUTE", "30"))

# Опциональный allowlist пользователей (defense-in-depth). По умолчанию пусто =
# реагируем на любого участника группы (модель «приватная группа = доверенные»).
# Задать в .env: ALLOWED_USER_IDS=123,456 — тогда бот слушает только этих юзеров.
_allowed_raw = os.getenv("ALLOWED_USER_IDS", "").strip()
ALLOWED_USER_IDS = {
    int(x) for x in re.split(r"[ ,]+", _allowed_raw)
    if x.strip().lstrip("-").isdigit()
} if _allowed_raw else set()

import agents_registry
from agents_registry import GROUP_CHAT_ID
from claude_runner import SubprocessRunner, AgentRunner, RunResult, StreamEvent, SETTINGS_PATH
from tg_stream_editor import TgStreamEditor
from codex_runner import CodexSubprocessRunner
from gemini_runner import GeminiSubprocessRunner
from deepseek_runner import DeepSeekRunner
from delegation_router import DelegationRouter
from session_manager import SessionManager, State
from conversation_log import log_event, log_delegation
from session_logger import log_session
from tg_formatter import format_for_telegram, split_for_telegram
from redact import redact_secrets
import bug_report
from error_classifier import classify_error, ErrorKind
from path_config import aios_root, bridge_log_dir, inbox_dir, queue_dir as get_queue_dir
import rate_guard


AIOS_ROOT = aios_root()
LOG_DIR = bridge_log_dir()
LOG_DIR.mkdir(parents=True, exist_ok=True)

# Лимиты на входящие файлы (защита от DoS большими/множественными вложениями)
MAX_INCOMING_FILE_MB = int(os.getenv("MAX_INCOMING_FILE_MB", "20"))   # TG bot API всё равно ~20MB
MAX_FILES_PER_MESSAGE = int(os.getenv("MAX_FILES_PER_MESSAGE", "10"))


def _is_within(path: Path, root: Path) -> bool:
    """True если path внутри root (надёжно, без ловушек startswith)."""
    try:
        return path.is_relative_to(root)
    except (ValueError, AttributeError):
        return str(path) == str(root) or str(path).startswith(str(root) + os.sep)


def _safe_filename(name: str) -> str:
    """Обезопасить имя входящего файла: только basename, без traversal, ограничение длины."""
    base = Path(name or "file").name  # срезает любые / и ..
    base = re.sub(r"[^\w.\- ()]+", "_", base, flags=re.UNICODE).strip() or "file"
    if len(base) > 120:
        stem, dot, ext = base.rpartition(".")
        base = (stem[:100] + dot + ext[:15]) if dot else base[:120]
    return base


# ────────────────────── logging ──────────────────────

def setup_logging() -> logging.Logger:
    logger = logging.getLogger("bridge")
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    fh = TimedRotatingFileHandler(
        LOG_DIR / "bridge.log", when="midnight", backupCount=30, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    # errors.log ротируется cron в 07:10 МСК (rotate-errors-log.sh) → не ротируем здесь
    ef = logging.FileHandler(LOG_DIR / "errors.log", encoding="utf-8")
    ef.setLevel(logging.ERROR)
    ef.setFormatter(fmt)
    logger.addHandler(ef)

    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    # Подавляем aiogram reconnect-спам (он идёт в journal через stderr)
    logging.getLogger("aiogram").setLevel(logging.ERROR)
    return logger


log = setup_logging()


# ────────────────────── lockfile (prevent double-polling) ──────────────────────

LOCK_PATH = Path(__file__).parent / ".bridge.lock"


def _acquire_lockfile() -> None:
    """
    PID-lockfile предотвращает двойной polling (watchdog restart race).
    Fingerprint = SHA256(sorted bot tokens)[:10] — разные наборы ботов не конфликтуют.
    """
    tokens = sorted(
        os.environ.get(cfg.bot_token_env or "", "")[:10]
        for cfg in agents_registry.enabled_agents()
        if cfg.bot_token_env
    )
    fingerprint = hashlib.sha256("|".join(tokens).encode()).hexdigest()[:10]
    pid = os.getpid()

    if LOCK_PATH.exists():
        try:
            data = json.loads(LOCK_PATH.read_text())
            old_pid = data.get("pid", 0)
            old_fp = data.get("fingerprint", "")
            if old_fp == fingerprint:
                try:
                    os.kill(old_pid, 0)
                    log.error(
                        "Another bridge (PID %d) is already running with same tokens. Exiting.",
                        old_pid,
                    )
                    sys.exit(1)
                except OSError:
                    log.warning("Stale lockfile (PID %d dead), taking over", old_pid)
            else:
                log.info("Lockfile has different fingerprint (%s vs %s), overwriting", old_fp, fingerprint)
        except Exception:
            log.warning("Corrupt lockfile, overwriting")

    LOCK_PATH.write_text(json.dumps({"pid": pid, "fingerprint": fingerprint}))
    atexit.register(lambda: LOCK_PATH.unlink(missing_ok=True))
    log.info("Lockfile acquired: PID=%d fingerprint=%s", pid, fingerprint)


_acquire_lockfile()


# ────────────────────── update deduplication ──────────────────────




# ────────────────────── runner pool ──────────────────────

class RunnerPool:
    def __init__(self):
        self._runners: dict[str, AgentRunner] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _build(self, name: str) -> AgentRunner:
        cfg = agents_registry.get(name)
        if cfg.runner_type == "claude":
            return SubprocessRunner(cfg.name, cfg.workdir, model=cfg.model, timeout_s=cfg.timeout_s, run_as_user=cfg.run_as_user, settings_path=cfg.settings_path or SETTINGS_PATH)
        if cfg.runner_type == "codex":
            return CodexSubprocessRunner(cfg.name, cfg.workdir, model=cfg.model, timeout_s=cfg.timeout_s)
        if cfg.runner_type == "gemini":
            return GeminiSubprocessRunner(cfg.name, cfg.workdir, model=cfg.model, timeout_s=cfg.timeout_s)
        if cfg.runner_type == "deepseek":
            return DeepSeekRunner(cfg.name, cfg.workdir, model=cfg.model, timeout_s=cfg.timeout_s)
        raise ValueError(f"unknown runner_type: {cfg.runner_type}")

    def get(self, name: str) -> AgentRunner:
        if name not in self._runners:
            self._runners[name] = self._build(name)
            self._locks[name] = asyncio.Lock()
        return self._runners[name]

    def get_codex_fallback(self, name: str) -> AgentRunner:
        """
        Fallback на Codex при Claude rate limit.
        Тот же workdir, те же файлы — другой CLI + модель.
        Ключ в кэше: "{name}__codex_fallback" чтобы не затереть основной runner.
        """
        fb_key = f"{name}__codex_fallback"
        if fb_key not in self._runners:
            cfg = agents_registry.get(name)
            self._runners[fb_key] = CodexSubprocessRunner(
                cfg.name, cfg.workdir,
                model="gpt-5.4-codex",
                timeout_s=cfg.timeout_s,
            )
        return self._runners[fb_key]

    def lock(self, name: str) -> asyncio.Lock:
        self.get(name)
        return self._locks[name]

    async def close_all(self) -> None:
        for r in self._runners.values():
            try:
                await r.close()
            except Exception:
                log.exception("runner close failed")



# ────────────────────── Bridge core ──────────────────────


@dataclass
class _QueuedInput:
    """Один пункт во входной очереди агента."""
    text: str
    caller: str
    topic_id: Optional[int] = None
    task_id: Optional[str] = None
    is_keepalive: bool = False
    is_forward: bool = False
    depth: int = 0  # глубина цепочки делегаций (0 = сообщение от пользователя)


# Дебаунс между подряд идущими msg от одного агента — склейка split-промпта.
INBOX_DEBOUNCE_S = float(os.getenv("INBOX_DEBOUNCE_S", "1.5"))
# Дебаунс для пересланных сообщений (пользователь пересылает 5 msg — ждём 1с после последнего)
FORWARD_DEBOUNCE_S = float(os.getenv("FORWARD_DEBOUNCE_S", "1.0"))
# Сколько живёт необработанный inline-approval до автоочистки (юзер не нажал кнопку).
APPROVAL_TTL_S = float(os.getenv("APPROVAL_TTL_S", "21600"))  # 6 часов
# Макс глубина цепочки делегаций (A→B→C→…). Дальше — стоп, чтобы исключить циклы.
MAX_DELEGATION_DEPTH = int(os.getenv("MAX_DELEGATION_DEPTH", "3"))
# Окно self-echo guard: сколько секунд помним отправленные агентом сообщения,
# чтобы не дать ему среагировать на собственный текст, вернувшийся как user_message.
SELF_ECHO_TTL_S = int(os.getenv("SELF_ECHO_TTL_S", "900"))
# Окно вокруг isolated-прогона агента: не-isolated user_message для того же агента
# в этом окне = его собственный self-post во время крона → дропаем (не реагируем).
ISOLATED_ECHO_WINDOW_S = int(os.getenv("ISOLATED_ECHO_WINDOW_S", "300"))


class Bridge:
    def __init__(self):
        workdirs = {name: cfg.workdir for name, cfg in agents_registry.AGENTS.items()}
        self.sessions = SessionManager(workdirs)
        self.runners = RunnerPool()
        self.router = DelegationRouter(agents_registry)
        self._bots: dict[str, object] = {}  # agent_name -> aiogram.Bot
        self._shutdown = asyncio.Event()
        # pending approvals: key → (agent_name, created_ts). created_ts нужен
        # чтобы чистить протухшие (юзер проигнорировал кнопку) — иначе словарь
        # растёт неограниченно за долгий uptime.
        self._pending_approvals: dict[str, tuple[str, float]] = {}
        self._bg_tasks: set[asyncio.Task] = set()  # background isolated runs
        # Per-agent input queue + worker : склейка split-промпта,
        # авто-flush сообщений накопленных пока агент BUSY.
        self._agent_queues: dict[str, asyncio.Queue] = {}
        self._agent_workers: dict[str, asyncio.Task] = {}
        # Self-echo guard: что агент сам отправил в чат за последние SELF_ECHO_TTL_S.
        # Нужно чтобы агент НЕ реагировал на собственные сообщения, если они
        # вернулись как user_message (типичная петля: cron-агент постит отчёт,
        # потом получает его обратно и пишет реакцию на свой же отчёт).
        self._recent_agent_posts: dict[str, list[tuple[float, str]]] = {}
        # Когда у агента был последний isolated-прогон (для self-post guard в очереди).
        self._isolated_run_ts: dict[str, float] = {}
        # Агенты с СЕЙЧАС идущим isolated-прогоном. Надёжный self-post guard,
        # не зависящий от длительности прогона (прогон может идти часами).
        self._isolated_active: set[str] = set()

    def register_bot(self, agent_name: str, bot) -> None:
        self._bots[agent_name] = bot

    @staticmethod
    def _norm_msg(text: str) -> str:
        """Нормализация для сравнения сообщений (убираем пробелы/регистр, берём начало)."""
        return re.sub(r"\s+", " ", text or "").strip().lower()[:300]

    def _record_agent_post(self, agent_name: str, text: str) -> None:
        """Запомнить что агент отправил в чат — для self-echo guard."""
        if not text or not text.strip():
            return
        now_ts = datetime.now(MSK).timestamp()
        lst = self._recent_agent_posts.setdefault(agent_name, [])
        lst.append((now_ts, self._norm_msg(text)))
        cutoff = now_ts - SELF_ECHO_TTL_S
        self._recent_agent_posts[agent_name] = [
            (ts, t) for (ts, t) in lst if ts >= cutoff
        ][-20:]

    def _is_self_echo(self, agent_name: str, text: str) -> bool:
        """True если text — это то, что агент сам недавно отправил в чат."""
        norm = self._norm_msg(text)
        if not norm:
            return False
        now_ts = datetime.now(MSK).timestamp()
        cutoff = now_ts - SELF_ECHO_TTL_S
        for ts, t in self._recent_agent_posts.get(agent_name, []):
            if ts < cutoff:
                continue
            if norm == t or norm.startswith(t[:120]) or t.startswith(norm[:120]):
                return True
        return False

    async def startup_announce(self) -> None:
        """После старта моста пишет в топик Сисадмина: один раз — welcome (с советом
        про запасной движок), плюс отчёт self-check (doctor) — всё ли настроено.
        На рестартах отчёт троттлится (раз в 30 мин), но при ошибках шлётся всегда."""
        sa = "sysadmin"
        cfg = agents_registry.AGENTS.get(sa)
        if not cfg or not getattr(cfg, "enabled", True) or not cfg.topic_id:
            log.info("startup_announce: Сисадмин не настроен (нет topic_id) — пропуск")
            return
        # ждём регистрацию бота Сисадмина (поллеры стартуют параллельно)
        for _ in range(30):
            if self._bots.get(sa) is not None:
                break
            if self._shutdown.is_set():
                return
            await asyncio.sleep(0.5)
        if self._bots.get(sa) is None:
            log.warning("startup_announce: бот Сисадмина не зарегистрирован — пропуск")
            return

        state_dir = LOG_DIR / "startup-state"
        state_dir.mkdir(parents=True, exist_ok=True)
        welcomed = state_dir / ".welcomed"
        report_marker = state_dir / ".last-startup-report"
        first_start = not welcomed.exists()

        # self-check через doctor
        report = "🩺 Проверка системы недоступна."
        has_errors = False
        try:
            from doctor import run_all
            checks = await run_all()
            ok = [c for c in checks if c.status == "ok"]
            warn = [c for c in checks if c.status == "warning"]
            err = [c for c in checks if c.status == "error"]
            has_errors = bool(err)
            head = f"🩺 Проверка системы: ✅ {len(ok)} ok · ⚠️ {len(warn)} · ❌ {len(err)}"
            problems = "\n".join(str(c) for c in (err + warn))
            report = head + (("\n\n" + problems) if problems else "\n\nВсё настроено и работает ✅")
        except Exception:
            log.exception("startup_announce: doctor failed")

        # троттлинг на рестартах: если не первый старт и нет ошибок — не чаще раз в 30 мин
        if not first_start and not has_errors:
            try:
                if report_marker.exists() and (time.time() - report_marker.stat().st_mtime) < 1800:
                    return
            except Exception:
                pass

        if first_start:
            msg = (
                "👋 Система установлена и запущена!\n\n"
                "Я — Сисадмин. Отсюда (топик ⚙️) ты управляешь всей AI-командой: "
                "создаёшь агентов, переключаешь движки, я чиню проблемы. "
                "Напиши «с чего начать» — расскажу подробнее.\n\n"
                "🔌 Сразу советую подключить **запасной движок** — лучше всего DeepSeek "
                "(это API: не разлогинивается, ~$5 хватит надолго). Если основной движок "
                "отвалится, система не встанет, и я останусь на связи. Скажи «хочу подключить DeepSeek».\n\n"
                + report
            )
        else:
            msg = "🔄 Мост перезапущен.\n\n" + report

        try:
            await self._send_from(sa, msg)
            welcomed.touch()
            report_marker.touch()
        except Exception:
            log.exception("startup_announce: send failed")

    def _schedule_restart(self, agent_name: str, reason: str) -> bool:
        """Записать «restart-note» и отложенно перезапустить мост.
        После рестарта restart_resume() вернётся к агенту и резюмит его сессию,
        чтобы он понимал, что чинил. Возвращает True если рестарт запланирован."""
        note = {"agent": agent_name, "reason": (reason or "")[:500], "ts": time.time()}
        try:
            (LOG_DIR / ".restart_resume.json").write_text(
                json.dumps(note, ensure_ascii=False), encoding="utf-8")
        except Exception:
            log.exception("schedule_restart: не смог записать restart-note")
        # Команда рестарта: systemd (Linux) / launchd (Mac). Отложенно (sleep), чтобы
        # успеть дослать ответ. Детачим в новую сессию.
        if sys.platform == "darwin":
            restart_cmd = f"launchctl kickstart -k gui/{os.getuid()}/com.aios-public.bridge"
        else:
            sudo = "" if os.geteuid() == 0 else "sudo "
            restart_cmd = f"{sudo}systemctl restart aios-bridge"
        try:
            subprocess.Popen(
                ["sh", "-c", f"sleep 3; {restart_cmd}"],
                start_new_session=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            log.info("[%s] restart scheduled (reason=%r)", agent_name, reason[:80])
            return True
        except Exception:
            log.exception("[%s] schedule_restart failed", agent_name)
            return False

    async def restart_resume(self) -> None:
        """После старта моста: если был self-restart, вернуться к инициировавшему
        агенту и резюмить его сессию (claude --resume через сохранённый session_id),
        напомнив причину — чтобы агент понимал, что чинил, и продолжил/подтвердил."""
        note_path = LOG_DIR / ".restart_resume.json"
        if not note_path.exists():
            return
        try:
            note = json.loads(note_path.read_text(encoding="utf-8"))
        except Exception:
            note_path.unlink(missing_ok=True)
            return
        # удаляем сразу — чтобы не зациклиться, если резюм-ран уронит мост
        note_path.unlink(missing_ok=True)
        agent = note.get("agent")
        reason = note.get("reason", "")
        ts = note.get("ts", 0)
        if not agent or agent not in agents_registry.AGENTS:
            return
        if time.time() - ts > 600:  # устаревшая нота — игнор
            return
        cfg = agents_registry.AGENTS.get(agent)
        if not cfg or not cfg.topic_id:
            return
        for _ in range(30):  # ждём регистрацию бота
            if self._bots.get(agent) is not None:
                break
            if self._shutdown.is_set():
                return
            await asyncio.sleep(0.5)
        if self._bots.get(agent) is None:
            return
        text = (
            f"🔄 Мост перезапущен — ты инициировал рестарт, чтобы применить: «{reason}». "
            "Сессия восстановлена (ты помнишь, что делал). Проверь, что всё поднялось "
            "(при необходимости — doctor), и подтверди пользователю, что готово, "
            "либо продолжи с места остановки."
        )
        await self._run_agent(agent, text, caller="user", topic_id=cfg.topic_id)

    async def deliver_user_message(self, agent_name: str, text: str, *, topic_id: int, is_forward: bool = False) -> None:
        # Агент НЕ должен реагировать на собственные сообщения, вернувшиеся как user_message.
        if self._is_self_echo(agent_name, text):
            log.warning("[%s] self-echo suppressed: собственное сообщение агента пришло как user_message (%d chars), не запускаю реакцию", agent_name, len(text))
            return
        log_event("user_msg", to=agent_name, topic=topic_id, text=text, source="user")
        await self._run_agent(agent_name, text, caller="user", topic_id=topic_id, is_forward=is_forward)

    async def deliver_agent_message(
        self, from_agent: str, to_agent: str, text: str,
        *, task_id: Optional[str] = None, kind: str = "delegation", depth: int = 0,
    ) -> None:
        log_event(
            "agent_msg",
            **{"from": from_agent, "to": to_agent, "task_id": task_id, "text": text, "kind": kind},
        )

        # Видимое сообщение от имени отправителя в топик получателя.
        # Пользователь видит живой диалог агентов: кто кому что написал.
        from_cfg = agents_registry.get(from_agent)
        to_cfg = agents_registry.get(to_agent)
        task_label = f" [{task_id}]" if task_id else ""
        if kind == "delegation":
            visible_header = f"📋 {from_cfg.display_name}{task_label}:\n\n"
        elif kind == "result":
            visible_header = f"✅ {from_cfg.display_name}{task_label}:\n\n"
        else:
            visible_header = f"💬 {from_cfg.display_name}{task_label}:\n\n"
        visible_body = text if text else "(пусто)"
        try:
            await self._send_from(from_agent, visible_header + visible_body, thread_id=to_cfg.topic_id)
        except Exception:
            log.exception("[%s→%s] visible delegation msg failed", from_agent, to_agent)

        prefix = f"[{kind.upper()} from {from_agent}"
        if task_id:
            prefix += f" / task={task_id}"
        prefix += "]\n"
        await self._run_agent(to_agent, prefix + text, caller=from_agent, task_id=task_id, depth=depth)

    async def _run_agent(
        self, agent_name: str, text: str,
        *, caller: str, topic_id: Optional[int] = None, task_id: Optional[str] = None,
        is_keepalive: bool = False, is_forward: bool = False, depth: int = 0,
    ) -> None:
        """
        Ставит сообщение в per-agent очередь.
        Если worker для агента ещё не запущен — стартует.
        Worker сам делает debounce (склейка split-промпта) + serial run.
        """
        cfg = agents_registry.get(agent_name)
        if not cfg.enabled:
            log.warning("[%s] disabled — message dropped", agent_name)
            return

        if agent_name not in self._agent_queues:
            self._agent_queues[agent_name] = asyncio.Queue()

        item = _QueuedInput(
            text=text, caller=caller, topic_id=topic_id,
            task_id=task_id, is_keepalive=is_keepalive,
            is_forward=is_forward, depth=depth,
        )
        await self._agent_queues[agent_name].put(item)
        log.info(
            "[%s] enqueued: caller=%s topic=%s keepalive=%s text=%r (qsize=%d)",
            agent_name, caller, topic_id, is_keepalive,
            text[:80], self._agent_queues[agent_name].qsize(),
        )

        # Стартуем worker если ещё нет (или старый умер)
        worker = self._agent_workers.get(agent_name)
        if worker is None or worker.done():
            self._agent_workers[agent_name] = asyncio.create_task(
                self._agent_worker(agent_name),
                name=f"worker-{agent_name}",
            )

    def _clear_busy(self, agent_name: str) -> None:
        """Снять BUSY если оно зависло (ранний выход/исключение до on_response)."""
        try:
            if self.sessions.load(agent_name).state == State.BUSY:
                self.sessions.on_busy(agent_name, False)
        except Exception:
            log.exception("[%s] clear_busy failed", agent_name)

    async def _agent_worker(self, agent_name: str) -> None:
        """
        Per-agent worker. Гарантирует:
          - serial запуск runs для одного агента (один claude-process в workdir)
          - debounce INBOX_DEBOUNCE_S — склейка split-промпта в один turn
          - auto-flush: msg накопленные пока run работал → следующий turn в той же сессии
          - keep-alive обрабатывается отдельно (без склейки с user-msg)
        """
        q = self._agent_queues[agent_name]
        log.info("[%s] worker started", agent_name)
        while not self._shutdown.is_set():
            try:
                first: _QueuedInput = await q.get()
            except asyncio.CancelledError:
                break

            # Keep-alive не склеиваем с user-msg — обрабатываем отдельно
            if first.is_keepalive:
                async with self.runners.lock(agent_name):
                    try:
                        await self._run_agent_actual(
                            agent_name, first.text,
                            caller=first.caller, topic_id=first.topic_id,
                            task_id=first.task_id, is_keepalive=True, depth=first.depth,
                        )
                    except Exception:
                        log.exception("[%s] worker: keepalive run failed", agent_name)
                    finally:
                        self._clear_busy(agent_name)
                continue

            # Обычный msg → дебаунс: ловим ещё msg в окне INBOX_DEBOUNCE_S,
            # склеиваем в один input. Keep-alive в окне — НЕ склеиваем,
            # обрабатываем отдельно после текущего батча.
            # forward coalescing — если первый msg = forward, используем
            # FORWARD_DEBOUNCE_S и РАСШИРЯЕМ окно при каждом новом forward.
            buffered: list[_QueuedInput] = [first]
            deferred_keepalive: Optional[_QueuedInput] = None
            has_forwards = first.is_forward
            debounce = FORWARD_DEBOUNCE_S if has_forwards else INBOX_DEBOUNCE_S
            deadline = asyncio.get_event_loop().time() + debounce
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    break
                try:
                    nxt: _QueuedInput = await asyncio.wait_for(q.get(), timeout=remaining)
                except asyncio.TimeoutError:
                    break
                if nxt.is_keepalive:
                    deferred_keepalive = nxt
                    break
                buffered.append(nxt)
                # каждый новый forward расширяет окно дебаунса
                if nxt.is_forward:
                    has_forwards = True
                    deadline = asyncio.get_event_loop().time() + FORWARD_DEBOUNCE_S

            # Drain всё что уже лежит — авто-flush сообщений накопленных
            # пока run был активен (но мы только что зашли в worker, lock ещё не взят —
            # значит реально это msg прилетевшие до debounce flush).
            try:
                while True:
                    more: _QueuedInput = q.get_nowait()
                    if more.is_keepalive:
                        if deferred_keepalive is None:
                            deferred_keepalive = more
                        else:
                            # уже один отложенный — этот ставим обратно в конец
                            q.put_nowait(more)
                        break
                    buffered.append(more)
            except asyncio.QueueEmpty:
                pass

            merged_text = (
                "\n\n".join(b.text for b in buffered)
                if len(buffered) > 1 else buffered[0].text
            )
            if len(buffered) > 1:
                log.info(
                    "[%s] worker: merged %d msgs → %d chars (split-promo / busy-queue)",
                    agent_name, len(buffered), len(merged_text),
                )

            async with self.runners.lock(agent_name):
                try:
                    await self._run_agent_actual(
                        agent_name, merged_text,
                        caller=buffered[0].caller, topic_id=buffered[0].topic_id,
                        task_id=buffered[0].task_id, is_keepalive=False, depth=buffered[0].depth,
                    )
                except Exception:
                    log.exception("[%s] worker: run failed", agent_name)
                finally:
                    self._clear_busy(agent_name)

                # Если за время run в очередь упали ещё msg — следующий
                # цикл while их подхватит и склеит как next turn (--resume
                # сохранит conversation context). Мы НЕ ждём здесь — просто
                # отпускаем lock и идём на следующую итерацию.

            # Отложенный keep-alive — обработать сразу после batch
            if deferred_keepalive:
                async with self.runners.lock(agent_name):
                    try:
                        await self._run_agent_actual(
                            agent_name, deferred_keepalive.text,
                            caller=deferred_keepalive.caller,
                            topic_id=deferred_keepalive.topic_id,
                            task_id=deferred_keepalive.task_id,
                            is_keepalive=True, depth=deferred_keepalive.depth,
                        )
                    except Exception:
                        log.exception("[%s] worker: deferred keepalive failed", agent_name)
                    finally:
                        self._clear_busy(agent_name)

        log.info("[%s] worker stopped", agent_name)

    def _build_fallback_runner(self, agent_name: str, provider: str, cfg):
        """Собрать раннер-замену для провайдера: claude→codex, codex→claude."""
        if provider == "claude":
            return self.runners.get_codex_fallback(agent_name)
        return SubprocessRunner(
            cfg.name, cfg.workdir, model="claude-sonnet-4-6",
            timeout_s=cfg.timeout_s, settings_path=cfg.settings_path or SETTINGS_PATH,
        )

    async def _o4mini_lastresort(self, agent_name: str, cfg, text: str, eff_topic):
        """Последняя попытка через o4-mini, когда Claude и Codex недоступны.
        Возвращает успешный RunResult или None (сообщение об ошибке уже отправлено)."""
        log.warning("[%s] trying o4-mini last-resort", agent_name)
        try:
            fb3 = CodexSubprocessRunner(cfg.name, cfg.workdir, model="o4-mini", timeout_s=cfg.timeout_s)
            r = await fb3.run(text, resume=False)
        except Exception:
            log.exception("[%s] o4-mini fallback failed", agent_name)
            await self._send_from(agent_name, "⚠️ Все провайдеры недоступны", thread_id=eff_topic)
            return None
        if r.error:
            await self._send_from(
                agent_name,
                f"⚠️ Все провайдеры недоступны: {redact_secrets(r.error)}",
                thread_id=eff_topic,
            )
            return None
        log.info("[%s] o4-mini fallback succeeded", agent_name)
        await self._send_from(
            agent_name, "⚠️ Claude и Codex недоступны. Ответ через o4-mini.", thread_id=eff_topic
        )
        return r

    async def _run_agent_actual(
        self, agent_name: str, text: str,
        *, caller: str, topic_id: Optional[int] = None, task_id: Optional[str] = None,
        is_keepalive: bool = False, depth: int = 0,
    ) -> None:
        cfg = agents_registry.get(agent_name)
        if not cfg.enabled:
            log.warning("[%s] disabled — message dropped", agent_name)
            return

        # Если вызов пришёл от другого агента — ответ идёт в топик этого агента,
        # а не в собственный. Это делает общение между агентами видимым в TG.
        if caller and caller != "user" and caller in agents_registry.AGENTS:
            caller_cfg = agents_registry.get(caller)
            eff_reply_topic = caller_cfg.topic_id
        else:
            eff_reply_topic = topic_id  # user-разговор: остаёмся в текущем топике

        self.sessions.on_incoming_message(agent_name)
        # BUSY на время выполнения tool call — чтобы /new и daily reset не сбросили
        # сессию посреди работы. Снимается в on_response (успех) или в finally воркера.
        self.sessions.on_busy(agent_name, True)
        runner = self.runners.get(agent_name)

        stored_sid = self.sessions.get_session_id(agent_name)
        use_streaming = (
            cfg.stream
            and not is_keepalive
            and hasattr(runner, "run_streaming")
        )

        log.info("[%s] starting run sid=%s keepalive=%s stream=%s text=%r",
                 agent_name, (stored_sid or "new")[:8], is_keepalive, use_streaming, text[:120])

        # smart pre-run guard — если provider заблокирован, сразу fallback
        _provider = cfg.runner_type  # "claude" / "codex" / "gemini" / "deepseek"
        _used_fallback = False
        if rate_guard.is_blocked(_provider) and _provider in ("claude", "codex"):
            _reason = rate_guard.get_reason(_provider)
            _remaining = rate_guard.remaining_s(_provider)
            _fb_label = "Codex 5.4" if _provider == "claude" else "Claude Sonnet"
            log.info("[%s] %s blocked (%s, %.0fs left), using %s", agent_name, _provider, _reason, _remaining, _fb_label)
            if _provider == "claude":
                runner = self.runners.get_codex_fallback(agent_name)
            else:
                runner = SubprocessRunner(
                    cfg.name, cfg.workdir, model="claude-sonnet-4-6",
                    timeout_s=cfg.timeout_s, settings_path=cfg.settings_path or SETTINGS_PATH,
                )
            stored_sid = None  # fallback — новая сессия
            _used_fallback = True
        elif rate_guard.is_blocked(_provider):
            # deepseek/gemini — нет fallback, просто ждём
            _wait = rate_guard.remaining_s(_provider)
            if _wait > 0:
                log.info("[%s] %s blocked, waiting %.1fs", agent_name, _provider, _wait)
                await asyncio.sleep(_wait)

        stream_editor = None
        try:
            if use_streaming and not _used_fallback:
                result, stream_editor = await self._run_with_streaming(
                    agent_name, runner, text, stored_sid, topic_id=eff_reply_topic,
                )
            else:
                result: RunResult = await runner.run(text, resume=True if not _used_fallback else False, session_id=stored_sid)
        except Exception as exc:
            log.exception("[%s] run failed", agent_name)
            log_event("error", agent=agent_name, error=str(exc))
            await self._send_from(agent_name, f"⚠️ Ошибка раннера: {exc}", thread_id=eff_reply_topic)
            return

        log_session(
            agent_name,
            session_id=result.session_id,
            model=cfg.model,
            usage=result.usage,
            cost_usd=result.cost_usd,
            duration_ms=result.duration_ms,
            error=result.error,
        )

        # persist session_id сразу после первого успешного turn
        if result.session_id:
            self.sessions.set_session_id(agent_name, result.session_id)

        if result.error:
            safe_err = redact_secrets(result.error)
            classified = classify_error(result.error)
            log.error("[%s] runner error [%s]: %s (hint: %s)",
                      agent_name, classified.kind.value, safe_err, classified.hint)
            log_event("error", agent=agent_name, error=safe_err, kind=classified.kind.value)

            # Баг-репорт: только НЕтранзиентные (UNKNOWN) — rate-limit/auth/overload это
            # операционные, не баги. Локально всегда; отправка — лишь при opt-in (.env).
            # Неблокирующе (executor), исключения внутри capture глушатся.
            if classified.kind == ErrorKind.UNKNOWN:
                try:
                    asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda: bug_report.capture(
                            source="error",
                            description=f"runner error in agent {agent_name}",
                            error=result.error, agent=agent_name, runner=_provider,
                            error_kind=classified.kind.value, version=BRIDGE_VERSION,
                        ),
                    )
                except Exception:
                    pass

            # умная реакция по типу ошибки
            if classified.kind == ErrorKind.RATE_LIMIT:
                rate_guard.record_limit(_provider, reason="rate_limit")
                _until = rate_guard.get_until_str(_provider)
                if _provider in ("claude", "codex"):
                    _fb_label = "Codex 5.4" if _provider == "claude" else "Claude Sonnet"
                    log.info("[%s] %s rate-limited, falling back to %s until %s", agent_name, _provider, _fb_label, _until)
                    await self._send_from(
                        agent_name,
                        f"⚠️ Rate limit {_provider.capitalize()}. Агент переключён на {_fb_label} до {_until} МСК.",
                        thread_id=eff_reply_topic,
                    )
                    try:
                        fb_runner = self._build_fallback_runner(agent_name, _provider, cfg)
                        result = await fb_runner.run(text, resume=False)
                    except Exception:
                        log.exception("[%s] primary fallback failed", agent_name)
                        result = None
                    if result is None or result.error:
                        # Основной fallback не помог — последний шанс через o4-mini
                        r = await self._o4mini_lastresort(agent_name, cfg, text, eff_reply_topic)
                        if r is None:
                            return
                        result = r
                    else:
                        log.info("[%s] fallback to %s succeeded", agent_name, _fb_label)
                else:
                    # deepseek/gemini — нет fallback, алерт + ждём
                    _wait_s = rate_guard.remaining_s(_provider)
                    await self._send_from(
                        agent_name,
                        f"⚠️ Rate limit {_provider}. Агент переключён в режим ожидания до {_until} МСК.",
                        thread_id=eff_reply_topic,
                    )
                    await asyncio.sleep(_wait_s)
                    try:
                        result = await runner.run(text, resume=True, session_id=stored_sid)
                        if not result.error:
                            log.info("[%s] retry after rate limit succeeded", agent_name)
                        else:
                            await self._send_from(agent_name, f"⚠️ {redact_secrets(result.error)}", thread_id=eff_reply_topic)
                            return
                    except Exception:
                        log.exception("[%s] retry after rate limit failed", agent_name)
                        return
            elif classified.kind == ErrorKind.CONTEXT_OVERFLOW and classified.should_compress:
                await self._send_from(
                    agent_name,
                    "📦 Контекст переполнен, сжимаю и повторяю...",
                    thread_id=eff_reply_topic,
                )
                try:
                    await runner.compact(session_id=stored_sid)
                    result = await runner.run(text, resume=True, session_id=stored_sid)
                    if not result.error:
                        log.info("[%s] retry after compact succeeded", agent_name)
                    else:
                        await self._send_from(agent_name, f"⚠️ {redact_secrets(result.error)}", thread_id=eff_reply_topic)
                        return
                except Exception:
                    log.exception("[%s] compact+retry failed", agent_name)
                    await self._send_from(agent_name, "⚠️ Компактизация не помогла", thread_id=eff_reply_topic)
                    return
            elif classified.should_retry and classified.kind in (ErrorKind.OVERLOADED, ErrorKind.SERVER_ERROR):
                wait_s = classified.retry_after_s or 10
                log.info("[%s] retrying in %ds (%s)", agent_name, wait_s, classified.kind.value)
                await asyncio.sleep(wait_s)
                try:
                    result = await runner.run(text, resume=True, session_id=stored_sid)
                    if not result.error:
                        log.info("[%s] retry after %s succeeded", agent_name, classified.kind.value)
                    else:
                        await self._send_from(agent_name, f"⚠️ {redact_secrets(result.error)}", thread_id=eff_reply_topic)
                        return
                except Exception:
                    log.exception("[%s] retry after %s failed", agent_name, classified.kind.value)
                    return
            elif classified.kind in (ErrorKind.AUTH, ErrorKind.AUTH_PERMANENT, ErrorKind.BILLING):
                # auth/billing — fallback + предупреждение + блокировка до :00/:30
                rate_guard.record_auth_error(_provider)
                _until = rate_guard.get_until_str(_provider)
                if _provider in ("claude", "codex") and not _used_fallback:
                    _fb_label = "Codex 5.4" if _provider == "claude" else "Claude Sonnet"
                    await self._send_from(
                        agent_name,
                        f"🚨 {_provider.capitalize()}: {classified.hint}\n"
                        f"Агент переключён на {_fb_label} до {_until} МСК. Разберись с аккаунтом.",
                        thread_id=eff_reply_topic,
                    )
                    try:
                        fb_runner = self._build_fallback_runner(agent_name, _provider, cfg)
                        result = await fb_runner.run(text, resume=False)
                        if not result.error:
                            log.info("[%s] auth fallback to %s succeeded", agent_name, _fb_label)
                        else:
                            await self._send_from(agent_name, f"⚠️ Fallback тоже fail: {redact_secrets(result.error)}", thread_id=eff_reply_topic)
                            return
                    except Exception:
                        log.exception("[%s] auth fallback failed", agent_name)
                        await self._send_from(agent_name, f"🚨 {safe_err}\n💡 {classified.hint}", thread_id=eff_reply_topic)
                        return
                else:
                    await self._send_from(agent_name, f"🚨 {safe_err}\n💡 {classified.hint}", thread_id=eff_reply_topic)
                    return
            elif classified.should_alert:
                await self._send_from(agent_name, f"🚨 {safe_err}\n\n💡 {classified.hint}", thread_id=eff_reply_topic)
                return
            else:
                await self._send_from(agent_name, f"⚠️ {safe_err}", thread_id=eff_reply_topic)
                return

        # continuation prompt — если ответ обрезан по output limit,
        # отправляем текущую часть и запрашиваем продолжение (макс 2 раза).
        if getattr(result, 'is_truncated', False) and not is_keepalive:
            log.info("[%s] response truncated, requesting continuation", agent_name)
            full_text = result.text or ""
            for _cont_i in range(2):
                cont_result = await runner.run(
                    "Твой предыдущий ответ был обрезан по лимиту. "
                    "Продолжи ровно с того места, где остановился. "
                    "Не повторяй уже сказанное.",
                    resume=True,
                    session_id=result.session_id,
                )
                if cont_result.text:
                    full_text += cont_result.text
                if cont_result.session_id:
                    result.session_id = cont_result.session_id
                    self.sessions.set_session_id(agent_name, cont_result.session_id)
                if not getattr(cont_result, 'is_truncated', False):
                    break
                log.info("[%s] continuation %d also truncated, retrying", agent_name, _cont_i + 1)
            result.text = full_text

        parsed = self.router.handle(agent_name, result.text)
        # диагностика: видеть что распарсилось из ответа
        log.info(
            "[%s] parsed: delegations=%d results=%d waiting=%s ask_user=%s tags=%s clean_chars=%d",
            agent_name,
            len(parsed["delegations"]),
            len(parsed["results"]),
            parsed["waiting_for"],
            parsed["ask_user"],
            [t.tag for t in parsed["raw_tags"]],
            len(parsed["clean_text"] or ""),
        )
        for _d in parsed["delegations"]:
            log.info("[%s] DELEGATE → %s task=%s body_chars=%d",
                     agent_name, _d["to"], _d["task_id"], len(_d["text"] or ""))
        # Опциональный дамп полного выхлопа агента (для отладки)
        if os.getenv("DUMP_AGENT_OUTPUTS", "0") == "1":
            try:
                dump_dir = LOG_DIR / "agent_outputs"
                dump_dir.mkdir(parents=True, exist_ok=True)
                ts = datetime.now(MSK).strftime("%Y%m%d-%H%M%S")
                dump_path = dump_dir / f"{agent_name}_{ts}.txt"
                dump_path.write_text(result.text or "", encoding="utf-8")
            except Exception:
                log.exception("[%s] failed to dump agent output", agent_name)

        if parsed["clean_text"]:
            if stream_editor:
                await stream_editor.finalize_with_clean(parsed["clean_text"])
            else:
                await self._send_from(agent_name, parsed["clean_text"], thread_id=eff_reply_topic)
            log_event(
                "agent_msg",
                **{"from": agent_name, "to": caller, "topic": eff_reply_topic or cfg.topic_id, "text": parsed["clean_text"]},
            )

        self.sessions.on_response(
            agent_name,
            waiting_for=parsed["waiting_for"] or None,
            waiting_reason=None,
        )

        if parsed["ask_user"]:
            await self._send_from(agent_name, "❓ Жду ответа от тебя.", thread_id=topic_id)
        for t in parsed["raw_tags"]:
            if t.tag == "APPROVAL_NEEDED":
                await self._send_approval_request(
                    agent_name,
                    t.body or "Агент запрашивает подтверждение действия",
                )

        # [MEMORY_UPDATE] — если агент передал тело тега, bridge сам пишет context.md.
        # Если тело пустое — агент уже сам обновил через Write tool, просто логируем.
        for t in parsed["raw_tags"]:
            if t.tag != "MEMORY_UPDATE":
                continue
            if t.body:
                ctx_file = cfg.workdir / "context.md"
                try:
                    ctx_file.write_text(t.body, encoding="utf-8")
                    log.info("[%s] context.md updated via MEMORY_UPDATE (%d chars)", agent_name, len(t.body))
                except Exception:
                    log.exception("[%s] failed to write context.md from MEMORY_UPDATE", agent_name)
            else:
                log.info("[%s] MEMORY_UPDATE received (agent updated context.md directly)", agent_name)

        # [FILE:/path] — агент хочет отправить файл пользователю
        for t in parsed["raw_tags"]:
            if t.tag != "FILE":
                continue
            file_path = t.args[0] if t.args else t.body.strip()
            caption = t.body.strip() if t.args else ""
            if file_path:
                await self._send_file(agent_name, file_path, caption=caption, thread_id=eff_reply_topic)

        # [RESTART] — агент просит перезапустить мост. Дослали ответ выше → планируем
        # рестарт; после старта restart_resume() вернётся к агенту и резюмит сессию.
        if parsed.get("restart_reason") is not None:
            _reason = parsed["restart_reason"] or "(без описания)"
            if self._schedule_restart(agent_name, _reason):
                await self._send_from(
                    agent_name,
                    "🔄 Перезапускаю мост (~10–15 сек). После старта вернусь к тебе "
                    "и подтвержу, что всё применилось — пропаду ненадолго.",
                    thread_id=eff_reply_topic,
                )
            else:
                await self._send_from(
                    agent_name,
                    "⚠️ Не смог перезапустить мост сам (нет прав?). Попроси пользователя "
                    "выполнить `bash $AIOS_ROOT/scripts/enable.sh` или "
                    "`sudo systemctl restart aios-bridge`.",
                    thread_id=eff_reply_topic,
                )

        for d in parsed["delegations"]:
            # Защита от циклов/бесконечной делегации: на глубине >= лимита — стоп.
            if depth >= MAX_DELEGATION_DEPTH:
                log.warning(
                    "[%s] delegation → %s blocked: max depth %d reached (chain too deep / possible cycle)",
                    agent_name, d["to"], MAX_DELEGATION_DEPTH,
                )
                await self._send_from(
                    agent_name,
                    f"⛔ Делегация остановлена: достигнута максимальная глубина "
                    f"цепочки ({MAX_DELEGATION_DEPTH}). Задачу нужно решить без "
                    f"дальнейшей передачи или вмешаться вручную.",
                    thread_id=eff_reply_topic,
                )
                continue
            log_delegation(d["from"], d["to"], d["task_id"], d["text"])
            asyncio.create_task(
                self.deliver_agent_message(
                    d["from"], d["to"], d["text"], task_id=d["task_id"],
                    kind="delegation", depth=depth + 1,
                )
            )

        for r in parsed["results"]:
            key = f"{agent_name}:{r['task_id']}" if r["task_id"] else agent_name
            self.sessions.on_waiting_resolved(r["to"], key)

            # Служебное "✅ завершил" в свой топик от своего имени
            to_cfg = agents_registry.get(r["to"])
            task_label = f" [{r['task_id']}]" if r["task_id"] else ""
            try:
                await self._send_from(
                    agent_name,
                    f"✅ Задачу{task_label} от {to_cfg.display_name} завершил, отправляю результат",
                )
            except Exception:
                log.exception("[%s] completion msg send failed", agent_name)

            asyncio.create_task(
                self.deliver_agent_message(
                    agent_name, r["to"], r["text"], task_id=r["task_id"],
                    kind="result", depth=depth + 1,
                )
            )

    async def _run_with_streaming(
        self,
        agent_name: str,
        runner: SubprocessRunner,
        text: str,
        stored_sid: Optional[str],
        topic_id: Optional[int] = None,
    ) -> tuple[RunResult, Optional[TgStreamEditor]]:
        """
        Потоковый запуск: стримим текст в TG через TgStreamEditor.
        Возвращает (RunResult, editor) — editor нужен для finalize_with_clean.
        topic_id: если задан — используется вместо cfg.topic_id (multi-topic агенты).
        """
        cfg = agents_registry.get(agent_name)
        bot = self._bots.get(agent_name)
        eff_chat_id = cfg.chat_id if cfg.chat_id else GROUP_CHAT_ID
        eff_thread = topic_id if topic_id is not None else cfg.topic_id
        editor = TgStreamEditor(bot, eff_chat_id, eff_thread) if bot else None
        result = None

        async for event in runner.run_streaming(text, resume=True, session_id=stored_sid):
            if event.kind == "text_delta" and editor:
                await editor.push(event.text)
            elif event.kind == "tool_start" and editor:
                await editor.push_tool(event.tool_name)
            elif event.kind == "result":
                result = event.result
                if editor:
                    await editor.clear_tools()

        if result is None:
            result = RunResult(text="", error="no result event from stream")

        return result, editor

    async def _run_agent_isolated(self, agent_name: str, text: str) -> None:
        """
        Одноразовый запуск агента без --resume и без мутации main-сессии.

        Используется для cron-триггеров: каждый запуск стартует с нуля,
        не загружает прежний session_id, не обновляет state machine.

        Отличия от _run_agent():
          - не вызывает sessions.on_incoming_message / on_response / set_session_id;
          - не парсит delegations / results / waiting_for (cron — не диалог);
          - тег [MEMORY_UPDATE] обрабатывается (изолированный агент тоже имеет
            право обновить свою память).
          - lock per-agent всё ещё берётся чтобы не пересечься с обычным run
            на той же workdir (claude CLI не любит параллельные процессы).
        """
        cfg = agents_registry.get(agent_name)
        if not cfg.enabled:
            log.warning("[%s] disabled — isolated trigger dropped", agent_name)
            return

        # Отмечаем окно isolated-прогона: если в это время прилетит не-isolated
        # user_message для этого же агента — это его собственный self-post
        # (агент пишет отчёт в очередь во время крон-прогона). Дропнем его в
        # _process_queue, чтобы агент не реагировал на свой же отчёт.
        self._isolated_run_ts[agent_name] = datetime.now(MSK).timestamp()

        lock = self.runners.lock(agent_name)
        async with lock:
            runner = self.runners.get(agent_name)
            self._isolated_run_ts[agent_name] = datetime.now(MSK).timestamp()
            log.info("[%s] ISOLATED run text=%r", agent_name, text[:120])
            try:
                result: RunResult = await runner.run_isolated(text)
            except Exception as exc:
                log.exception("[%s] isolated run failed", agent_name)
                log_event("error", agent=agent_name, error=f"isolated: {exc}")
                await self._send_from(agent_name, f"⚠️ Ошибка изолированного запуска: {exc}")
                return

            log_session(
                agent_name,
                session_id=result.session_id,
                model=cfg.model,
                usage=result.usage,
                cost_usd=result.cost_usd,
                duration_ms=result.duration_ms,
                error=result.error,
                mode="isolated",
            )

            if result.error:
                safe_err = redact_secrets(result.error)
                log.error("[%s] isolated runner error: %s", agent_name, safe_err)
                log_event("error", agent=agent_name, error=safe_err)
                await self._send_from(agent_name, f"⚠️ {safe_err}")
                return

            # Парсим только чтобы вытащить clean_text и MEMORY_UPDATE.
            # Делегации/результаты/waiting_for игнорируем — это cron, не диалог.
            parsed = self.router.handle(agent_name, result.text)

            if parsed["clean_text"]:
                text = parsed["clean_text"]
                # Срез self-narration isolated-вывода. Промпт нередко навязывает
                # агенту роль «отправителя в TG», и он оборачивает отчёт в рекап
                # «Отправлено. Вот что ушло в TG (топик N): … <дубль отчёта>» либо
                # ставит такую строку ПЕРЕД отчётом. Мост — единственный, кто
                # постит, поэтому такой нарратив всегда лишний и выглядит как
                # «агент отвечает сам себе». Чистим на уровне моста (head + tail),
                # а не промптами каждого агента.

                # head: ведущая строка «Отчёт отправлен в TG (topic …)» перед телом
                _lead_narr_re = re.compile(
                    r"^\s*(?:"
                    r"(?:Отчёт|Отчет|Сообщение|Сводка|Дайджест|Пост[а-яё]*|Анонс)\s+отправлен[а-яё]*\s+в\s+(?:TG|топик|тред|thread|телеграм)"
                    r"|Отправлено\s+в\s+(?:TG|топик|тред|thread)"
                    r"|Опубликовано\s+в\s+топик"
                    r")[^\n]*\n+",
                    re.IGNORECASE,
                )
                _lead_m = _lead_narr_re.match(text)
                if _lead_m and (len(text) - _lead_m.end()) > 50:
                    log.info("[%s] isolated: stripped lead narration (%d chars)", agent_name, _lead_m.end())
                    text = text[_lead_m.end():].lstrip()

                # tail: рекап-маркер + продублированный отчёт после него
                _echo_tail_re = re.compile(
                    r"(?:\n+|^)\s*(?:"
                    r"Отправлено[.!]?\s*Вот что"
                    r"|Вот что ушло в\s*(?:TG|телеграм|топик)"
                    r"|Отправлено в топик"
                    r"|Сообщение отправлено в"
                    r"|Сводка отправлена"
                    r"|Отчёт(?:\s+\S+){0,4}\s+отправлен"
                    r"|Скан завершён"
                    r"|Готово[.!]?\s*(?:Итог|Результат|Вот что (?:сделал|сделано|нашёл|отправил))"
                    r"|Вот что (?:сделал|сделано|нашёл|отправил|ушло)"
                    r")",
                    re.IGNORECASE,
                )
                _echo_match = _echo_tail_re.search(text)
                if _echo_match and _echo_match.start() > 100:
                    log.info("[%s] isolated: stripped self-echo tail (%d chars)",
                             agent_name, len(text) - _echo_match.start())
                    text = text[:_echo_match.start()].rstrip()

                # Suppress full-housekeeping messages (summaries, context compression).
                # ВАЖНО: глушим только КОРОТКИЕ служебные ответы. Реальные отчёты
                # длинные (>400 симв) и могут начинаться с "Готово. Вот что нашёл..." —
                # их подавлять нельзя, иначе отчёт не доедет до чата.
                _housekeeping = len(text) < 400 and (
                    re.match(r"^Готово\.?\s*(Саммари|Context|context|Итог|Вот что)", text, re.IGNORECASE)
                    or re.match(r"^Саммари сессии", text, re.IGNORECASE)
                    or re.match(r"^(Готово|Done|Выполнено)\.?\s*$", text.strip(), re.IGNORECASE)
                )
                if _housekeeping:
                    log.info("[%s] isolated: suppressed housekeeping msg (%d chars)", agent_name, len(text))
                else:
                    await self._send_from(agent_name, text)
                log_event(
                    "agent_msg",
                    **{"from": agent_name, "to": "cron", "topic": cfg.topic_id, "text": text},
                )

            for t in parsed["raw_tags"]:
                if t.tag != "MEMORY_UPDATE":
                    continue
                if t.body:
                    ctx_file = cfg.workdir / "context.md"
                    try:
                        ctx_file.write_text(t.body, encoding="utf-8")
                        log.info("[%s] context.md updated via MEMORY_UPDATE (isolated)", agent_name)
                    except Exception:
                        log.exception("[%s] failed to write context.md (isolated)", agent_name)

    async def _send_from(self, agent_name: str, text: str, *, thread_id: Optional[int] = None) -> None:
        """
        Отправить сообщение в TG-топик от имени конкретного агента.

        Pipeline:
          1. split_for_telegram(text, 3800)   — режем ДО конвертации,
             чтобы не разорвать HTML-теги
          2. format_for_telegram(chunk)        — markdown → TG HTML
          3. bot.send_message(parse_mode=HTML) — отправка
          4. При ошибке HTML-парсинга — fallback в plain text

        thread_id: если задан — используется вместо cfg.topic_id (для multi-topic агентов).
        """
        cfg = agents_registry.get(agent_name)
        bot = self._bots.get(agent_name)
        eff_chat_id = cfg.chat_id if cfg.chat_id else GROUP_CHAT_ID
        eff_thread = thread_id if thread_id is not None else cfg.topic_id
        # Запоминаем что агент сам отправил — чтобы не среагировать на это,
        # если текст вернётся как user_message (self-echo guard).
        self._record_agent_post(agent_name, text)
        if bot is None:
            log.info("[TG-DRY/%s] chat=%s topic=%s text=%s", agent_name, eff_chat_id, eff_thread, text[:200])
            return

        chunks = split_for_telegram(text, limit=3800)

        for chunk in chunks:
            if not chunk.strip():
                continue
            try:
                formatted = format_for_telegram(chunk)
            except Exception:
                log.exception("[%s] formatter crashed, falling back to plain", agent_name)
                formatted = chunk

            try:
                await bot.send_message(
                    chat_id=eff_chat_id,
                    message_thread_id=eff_thread,
                    text=formatted,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except Exception as exc:
                # HTML может оказаться невалидным (незакрытый тег, спорная вложенность).
                # В этом случае — fallback в plain text, чтобы сообщение всё равно дошло.
                log.warning("[%s] HTML send failed (%s), retry plain", agent_name, exc)
                try:
                    await bot.send_message(
                        chat_id=eff_chat_id,
                        message_thread_id=eff_thread,
                        text=chunk,
                        parse_mode=None,
                        disable_web_page_preview=True,
                    )
                except Exception:
                    log.exception("[%s] plain send also failed", agent_name)


    async def _send_file(self, agent_name: str, file_path: str, caption: str = "", *, thread_id: int | None = None) -> None:
        """
        Отправить файл в TG-топик от имени агента.

        Безопасность: файл должен быть в {AIOS_ROOT}/ или /tmp/.
        Картинки (jpg/png/gif/webp) → send_photo, остальное → send_document.
        """
        cfg = agents_registry.get(agent_name)
        bot = self._bots.get(agent_name)
        eff_chat_id = cfg.chat_id if cfg.chat_id else GROUP_CHAT_ID
        eff_thread = thread_id if thread_id is not None else cfg.topic_id

        p = Path(file_path)
        if not p.exists():
            log.warning("[%s] FILE tag path does not exist: %s", agent_name, file_path)
            await self._send_from(agent_name, f"⚠️ Файл не найден: {file_path}", thread_id=eff_thread)
            return

        resolved = p.resolve()
        # Разрешённые корни (надёжная проверка через is_relative_to, не startswith,
        # чтобы /tmpfoo не прошёл как /tmp).
        allowed_roots = [AIOS_ROOT.resolve(), Path("/tmp").resolve()]
        if not any(_is_within(resolved, r) for r in allowed_roots):
            log.warning("[%s] FILE tag path outside allowed dirs: %s", agent_name, resolved)
            await self._send_from(agent_name, f"⚠️ Отправка файлов разрешена только из {AIOS_ROOT}/ и /tmp/", thread_id=eff_thread)
            return

        # Запрет на секреты/служебные файлы даже внутри AIOS_ROOT
        # (например Сисадмин не должен слить .env в чат).
        _name = resolved.name
        _rel = str(resolved)
        if (_name in (".env", ".state", ".bridge.lock", ".rate_limits.json")
                or _name.endswith(".env")
                or "/bridge/.env" in _rel
                or "/logs/" in _rel
                or "/.ssh/" in _rel
                or "credential" in _name.lower()):
            log.warning("[%s] FILE tag blocked (secret/service file): %s", agent_name, resolved)
            await self._send_from(agent_name, "⚠️ Этот файл отправлять нельзя (секреты/служебные данные).", thread_id=eff_thread)
            return

        if bot is None:
            log.info("[TG-DRY/%s] would send file: %s", agent_name, file_path)
            return

        from aiogram.types import FSInputFile
        tg_file = FSInputFile(str(resolved))
        image_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
        cap = caption[:1024] if caption else None

        try:
            if p.suffix.lower() in image_exts:
                await bot.send_photo(
                    chat_id=eff_chat_id,
                    message_thread_id=eff_thread,
                    photo=tg_file,
                    caption=cap,
                )
            else:
                await bot.send_document(
                    chat_id=eff_chat_id,
                    message_thread_id=eff_thread,
                    document=tg_file,
                    caption=cap,
                )
            log.info("[%s] file sent: %s (%d bytes)", agent_name, p.name, p.stat().st_size)
        except Exception:
            log.exception("[%s] failed to send file %s", agent_name, file_path)
            await self._send_from(agent_name, f"⚠️ Не удалось отправить файл: {p.name}", thread_id=eff_thread)

    async def send_keepalive(self, agent_name: str) -> None:
        """
        Auto-пинг для WAITING агентов: попроси короткий статус-апдейт.
        Две цели:
          1) освежить prompt-кэш Anthropic (TTL 1h) — длинные WAITING не теряли контекст
          2) пользователь получает прогресс в свой топик раз в 30 мин
        """
        st = self.sessions.load(agent_name)
        waiting_info = ""
        if st.waiting_for:
            waiting_info = (
                f"\n\nТы сейчас в состоянии WAITING — ждёшь: {', '.join(st.waiting_for)}.\n"
                f"ВАЖНО: в конце ответа повтори теги [WAITING_FOR:<id>] для каждого из них, "
                f"чтобы сохранить статус WAITING. Без этого bridge сбросит тебя в ACTIVE."
            )
        text = (
            "[SYSTEM KEEP-ALIVE] Сделай апдейт по процессу: всё ли работает, "
            "как дела, когда планируется завершение. Ответь коротко — "
            "1-3 предложения, без воды. Новую работу не стартуй." + waiting_info
        )
        log_event("keepalive", to=agent_name, waiting_for=list(st.waiting_for))
        log.info("[%s] keep-alive ping (waiting_for=%s)", agent_name, st.waiting_for)
        await self._run_agent(agent_name, text, caller="system", is_keepalive=True)

    # ---------- фоновые задачи ----------

    async def _process_queue(self) -> None:
        """
        Читает JSON-файлы из bridge/queue/, выполняет команды, удаляет файлы.

        Форматы:
          {"action": "user_message", "agent": "priya", "text": "..."}
          {"action": "user_message", "agent": "priya", "text": "...", "isolated": true}
          {"action": "daily_reset"}
        """
        queue_dir = get_queue_dir()
        if not queue_dir.exists():
            return
        for fpath in sorted(queue_dir.glob("*.json")):
            try:
                data = json.loads(fpath.read_text(encoding="utf-8"))
            except Exception:
                log.exception("queue: failed to read %s", fpath.name)
                fpath.unlink(missing_ok=True)
                continue
            action = data.get("action", "")
            log.info("queue: processing %s action=%s", fpath.name, action)
            try:
                if action == "user_message":
                    agent = data.get("agent", "")
                    text = data.get("text", "")
                    isolated = bool(data.get("isolated", False))
                    if agent and text and agent in agents_registry.AGENTS:
                        if isolated:
                            # Отмечаем окно isolated-прогона СРАЗУ (до create_task),
                            # чтобы self-post этого же агента, прилетевший в это окно,
                            # гарантированно дропнулся даже если он в том же tick.
                            self._isolated_run_ts[agent] = datetime.now(MSK).timestamp()
                            # Помечаем активный прогон ДО запуска — снимется в
                            # done-callback (и при успехе, и при исключении).
                            self._isolated_active.add(agent)
                            # Fire-and-forget: не блокируем maintenance_loop
                            task = asyncio.create_task(
                                self._run_agent_isolated(agent, text),
                                name=f'isolated-{agent}-{fpath.stem}',
                            )
                            self._bg_tasks.add(task)

                            def _iso_done(t, _a=agent):
                                self._bg_tasks.discard(t)
                                self._isolated_active.discard(_a)
                                # grace-окно self-post считаем от ЗАВЕРШЕНИЯ прогона
                                self._isolated_run_ts[_a] = datetime.now(MSK).timestamp()

                            task.add_done_callback(_iso_done)
                        else:
                            # Self-post guard: не-isolated user_message для агента,
                            # который прямо сейчас в окне своего isolated-прогона —
                            # это его собственный отчёт, выложенный в очередь. Не
                            # подаём его агенту как входящее (иначе ответит на свой
                            # же отчёт). Отчёт и так уходит в чат авто-постом.
                            _iso_ts = self._isolated_run_ts.get(agent, 0)
                            _iso_running = agent in self._isolated_active
                            if _iso_running or (datetime.now(MSK).timestamp() - _iso_ts < ISOLATED_ECHO_WINDOW_S):
                                log.warning("[%s] self-post в очереди (isolated %s) — дропнут (%d chars), реакция не запускается",
                                            agent, "идёт" if _iso_running else "только что завершён", len(text))
                                fpath.unlink(missing_ok=True)
                                continue
                            cfg = agents_registry.get(agent)
                            await self.deliver_user_message(agent, text, topic_id=cfg.topic_id)
                    else:
                        log.warning("queue: invalid user_message payload: %s", data)
                elif action == "daily_reset":
                    await self._daily_reset_all()
                else:
                    log.warning("queue: unknown action %r in %s", action, fpath.name)
            except Exception:
                log.exception("queue: error processing %s", fpath.name)
            fpath.unlink(missing_ok=True)

    async def maintenance_loop(self, *, interval_s: int = 60) -> None:
        keepalive_min = int(os.getenv("KEEPALIVE_MIN", "30"))
        keepalive_cap = int(os.getenv("KEEPALIVE_MAX", "55"))
        while not self._shutdown.is_set():
            try:
                # hot-reload agents.toml
                agents_registry.reload_if_changed()

                # Обработка файловой очереди (trigger.py → cron агенты)
                await self._process_queue()

                # Чистка протухших inline-approval'ов (юзер проигнорировал кнопку)
                if self._pending_approvals:
                    _now_ts = datetime.now(MSK).timestamp()
                    _stale = [
                        k for k, (_, ts) in self._pending_approvals.items()
                        if _now_ts - ts > APPROVAL_TTL_S
                    ]
                    for k in _stale:
                        self._pending_approvals.pop(k, None)
                    if _stale:
                        log.info("purged %d stale pending approvals", len(_stale))

                # Daily reset в 5-минутном окне начиная с DAILY_RESET_HOUR:MINUTE
                now_msk = datetime.now(LOCAL_TZ)
                if now_msk.hour == DAILY_RESET_HOUR and DAILY_RESET_MINUTE <= now_msk.minute < DAILY_RESET_MINUTE + 5:
                    await self._daily_reset_all()

                for name, cfg in agents_registry.AGENTS.items():
                    if not cfg.enabled:
                        continue
                    # skip agents added by hot-reload but not yet in session_manager
                    if name not in self.sessions._workdirs:
                        continue
                    # WAITING keep-alive: каждые 30 мин пингуем агента чтобы кэш не умер
                    st = self.sessions.load(name)
                    if st.state == State.WAITING and st.last_activity:
                        from datetime import datetime as _dt
                        age_s = (_dt.now(self.sessions._parse(st.last_activity).tzinfo)
                                 - self.sessions._parse(st.last_activity)).total_seconds()
                        age_min = age_s / 60
                        if keepalive_min <= age_min <= keepalive_cap:
                            log.info("[%s] keep-alive due (idle %.1f min)", name, age_min)
                            await self.send_keepalive(name)
                            continue  # остальное пропустим этот tick
                    # Codex/Gemini-агенты не имеют persistent session — compact бесполезен.
                    # Им хватает ежедневного daily reset.
                    if cfg.runner_type in ("codex", "gemini", "deepseek"):
                        continue
                    timeout = cfg.idle_timeout_min or 60
                    if self.sessions.needs_compact(name, idle_timeout_min=timeout):
                        stored_sid = self.sessions.get_session_id(name)
                        log.info("[%s] auto-compact (idle %d min, sid=%s)", name, timeout,
                                 (stored_sid or "none")[:8])
                        try:
                            runner = self.runners.get(name)
                            await runner.compact(session_id=stored_sid)
                        except Exception:
                            log.exception("[%s] compact failed", name)
                        self.sessions.on_compact(name)
                    if self.sessions.waiting_stale(name, max_hours=24):
                        log.warning("[%s] WAITING >24h, escalating", name)
                        await self._send_from(name, "⚠️ Ожидание >24ч. Проверь статус.")
            except Exception:
                log.exception("maintenance loop error")
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=interval_s)
            except asyncio.TimeoutError:
                pass

    async def _send_approval_request(self, agent_name: str, description: str) -> None:
        """
        Отправить сообщение с inline-кнопками ✅/❌ вместо plain text.
        Ключ pending_approval сохраняется в self._pending_approvals.
        """
        try:
            from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        except ImportError:
            log.warning("[%s] aiogram InlineKeyboard unavailable — fallback to plain", agent_name)
            await self._send_from(agent_name, f"🟡 Нужен апрув:\n{description}")
            return

        cfg = agents_registry.get(agent_name)
        bot = self._bots.get(agent_name)
        if bot is None:
            log.warning("[%s] no bot registered, cannot send approval", agent_name)
            return

        now_ts = datetime.now(MSK).timestamp()
        key = f"{agent_name}:{int(now_ts)}"
        self._pending_approvals[key] = (agent_name, now_ts)

        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"appr:yes:{key}"),
            InlineKeyboardButton(text="❌ Отклонить",   callback_data=f"appr:no:{key}"),
        ]])

        body = description[:800]  # TG ограничение
        text_html = format_for_telegram(f"🟡 **Нужен апрув**\n\n{body}")
        try:
            eff_chat_id_appr = cfg.chat_id if cfg.chat_id else GROUP_CHAT_ID
            await bot.send_message(
                chat_id=eff_chat_id_appr,
                message_thread_id=cfg.topic_id,
                text=text_html,
                parse_mode="HTML",
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
            log.info("[%s] approval request sent (key=%s)", agent_name, key)
        except Exception:
            log.exception("[%s] approval request send failed", agent_name)

    async def handle_approval_callback(
        self, key: str, approved: bool, callback
    ) -> None:
        """
        Обработать нажатие inline-кнопки (✅/❌).
        Убирает кнопки, обновляет сообщение, отправляет результат агенту.
        """
        _entry = self._pending_approvals.pop(key, None)
        agent_name = _entry[0] if _entry else None

        # Убрать кнопки из TG-сообщения
        try:
            verdict_line = "✅ Подтверждено" if approved else "❌ Отклонено"
            original = callback.message.text or ""
            await callback.message.edit_text(
                f"{verdict_line}\n\n{original}",
                parse_mode=None,
                reply_markup=None,
            )
        except Exception:
            log.warning("approval: failed to edit message")

        if not agent_name:
            log.warning("approval: key %s not found in pending", key)
            return

        cfg = agents_registry.get(agent_name)
        if approved:
            reply = "✅ Пользователь подтвердил действие. Продолжай выполнение."
        else:
            reply = "❌ Пользователь отклонил действие. Остановись и уточни дальнейшие шаги."

        log.info("[%s] approval %s", agent_name, "YES" if approved else "NO")
        await self.deliver_user_message(agent_name, reply, topic_id=cfg.topic_id)

    async def _daily_reset_all(self) -> None:
        """
        Ежедневный сброс сессий (время задаётся DAILY_RESET_HOUR/MINUTE):
          1. Snapshot context.md → context_archive/YYYY-MM-DD.md
          2. on_reset_to_idle — сессия сбрасывается, агент начинает день чисто.
          WAITING агенты и BUSY — пропускаются.
        """
        today = datetime.now(MSK).strftime("%Y-%m-%d")
        for name, cfg in agents_registry.AGENTS.items():
            if not cfg.enabled:
                continue
            st = self.sessions.load(name)
            # Уже делали сегодня
            if st.last_reset and st.last_reset[:10] == today:
                continue
            if not self.sessions.can_daily_reset(name):
                log.info("[%s] daily reset skipped (state=%s waiting=%s)", name, st.state, st.waiting_for)
                continue
            # Snapshot context.md
            ctx_file = cfg.workdir / "context.md"
            if ctx_file.exists():
                archive_dir = cfg.workdir / "context_archive"
                archive_dir.mkdir(exist_ok=True)
                shutil.copy2(ctx_file, archive_dir / f"{today}.md")
                log.info("[%s] context.md snapshot → context_archive/%s.md", name, today)
            # session summary для personal-агентов перед daily reset
            # Скипаем если агент не получал реальных сообщений — чтобы не слать пустые саммари
            # NB: НЕ завязываемся на real_turns_since_compact — ночная idle-компакция
            # обнуляет этот счётчик ДО reset, из-за чего саммари ошибочно скипалось,
            # даже если за день был реальный разговор. Достаточно last_activity > last_reset.
            _had_real_activity = (
                st.session_id
                and st.last_activity
                and (not st.last_reset or st.last_activity > st.last_reset)
            )
            if name.startswith("personal_") and _had_real_activity:
                try:
                    log.info("[%s] requesting session summary before daily reset", name)
                    await self._run_agent(
                        name,
                        "[SESSION_SUMMARY] Ночной сброс. Обнови JSON-хранилища — "
                        "запиши всё из текущей сессии что ещё не записано. "
                        "Ответь коротким саммари.",
                        caller="system",
                        topic_id=cfg.topic_id,
                    )
                    # Ждём завершения (макс 90 сек)
                    for _ in range(60):  # 120 sec timeout for daily session summary
                        await asyncio.sleep(2)
                        st_check = self.sessions.load(name)
                        if st_check.state != State.BUSY:
                            break
                    log.info("[%s] session summary before daily reset completed", name)
                except Exception:
                    log.exception("[%s] session summary before daily reset failed", name)

            # Сброс сессии
            self.sessions.on_reset_to_idle(name)
            # КРИТично: on_reset_to_idle чистит session_id только в state.json,
            # но в раннере остаётся in-memory self._last_session_id. Без сброса
            # следующий запуск делает `sid = session_id or self._last_session_id`
            # → --resume вчерашней сессии, и агент «помнит» прошлый день. Чистим раннер.
            try:
                runner = self.runners.get(name)
                if runner is not None and hasattr(runner, "reset"):
                    _res = runner.reset()
                    if asyncio.iscoroutine(_res):
                        await _res
            except Exception:
                log.exception("[%s] runner reset on daily reset failed", name)
            log.info("[%s] daily reset done", name)

    async def shutdown(self) -> None:
        self._shutdown.set()
        # Останавливаем per-agent workers
        for name, worker in list(self._agent_workers.items()):
            if not worker.done():
                worker.cancel()
        for name, worker in list(self._agent_workers.items()):
            try:
                await asyncio.wait_for(worker, timeout=5)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass
        await self.runners.close_all()


# ────────────────────── file attachments ──────────────────────

INBOX_ROOT = inbox_dir()


def _collect_attachments(message) -> list:
    """Вернуть список TG-объектов-файлов из сообщения (document/photo/voice/audio/video)."""
    found = []
    if message.document:
        found.append(("document", message.document))
    if message.photo:
        # photo — массив размеров, берём самое большое
        found.append(("photo", message.photo[-1]))
    if message.voice:
        found.append(("voice", message.voice))
    if message.audio:
        found.append(("audio", message.audio))
    if message.video:
        found.append(("video", message.video))
    if message.video_note:
        found.append(("video_note", message.video_note))
    return found


async def _save_attachment(bot, attach_tuple, agent_name: str) -> str:
    """Скачать вложение в workspace/temp/inbox/<agent>/, вернуть абсолютный путь."""
    kind, tg_obj = attach_tuple
    inbox = INBOX_ROOT / agent_name
    inbox.mkdir(parents=True, exist_ok=True)

    # Лимит размера: TG bot API сам ~20MB, но проверяем явно где есть file_size
    size = getattr(tg_obj, "file_size", None)
    if size and size > MAX_INCOMING_FILE_MB * 1024 * 1024:
        log.warning("[%s] attachment too large: %s bytes (limit %dMB)", agent_name, size, MAX_INCOMING_FILE_MB)
        return ""

    # выбрать имя файла (санитизировать — Telegram file_name произвольный)
    filename = getattr(tg_obj, "file_name", None)
    if not filename:
        # photo / voice / etc — генерируем
        ext_map = {"photo": ".jpg", "voice": ".ogg", "audio": ".mp3",
                   "video": ".mp4", "video_note": ".mp4"}
        ext = ext_map.get(kind, "")
        filename = f"{kind}_{tg_obj.file_unique_id}{ext}"
    filename = _safe_filename(filename)

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = inbox / f"{ts}_{filename}"
    # Финальная защита: путь обязан остаться внутри inbox
    if not _is_within(dest.resolve(), inbox.resolve()):
        log.warning("[%s] attachment path escapes inbox: %s", agent_name, dest)
        return ""

    await bot.download(tg_obj, destination=dest)
    log.info("[%s] saved attachment: %s", agent_name, dest)
    return str(dest)


async def _transcribe_voice(file_path: str) -> str:
    """Transcribe voice/audio via AssemblyAI (no diarization, cheaper)."""
    api_key = os.environ.get("ASSEMBLYAI_API_KEY", "")
    if not api_key:
        log.warning("ASSEMBLYAI_API_KEY not set, skipping transcription")
        return ""

    import aiohttp

    headers = {"authorization": api_key}
    base = "https://api.assemblyai.com/v2"

    try:
        async with aiohttp.ClientSession() as session:
            # Upload file
            with open(file_path, "rb") as fh:
                async with session.post(
                    f"{base}/upload", headers=headers, data=fh
                ) as resp:
                    if resp.status != 200:
                        log.error("AssemblyAI upload failed: %d", resp.status)
                        return ""
                    upload_url = (await resp.json())["upload_url"]

            # Request transcription (no diarization = cheaper)
            async with session.post(
                f"{base}/transcript",
                headers=headers,
                json={"audio_url": upload_url, "language_detection": True, "speech_models": ["universal-2"]},
            ) as resp:
                if resp.status != 200:
                    log.error("AssemblyAI transcript request failed: %d", resp.status)
                    return ""
                transcript_id = (await resp.json())["id"]

            # Poll until done (usually 5-15 sec for short voice)
            for _ in range(60):
                await asyncio.sleep(2)
                async with session.get(
                    f"{base}/transcript/{transcript_id}", headers=headers
                ) as resp:
                    data = await resp.json()
                    status = data.get("status")
                    if status == "completed":
                        return data.get("text", "")
                    if status == "error":
                        log.error("AssemblyAI error: %s", data.get("error"))
                        return ""

        log.warning("AssemblyAI timeout for %s", file_path)
        return ""
    except Exception:
        log.exception("Voice transcription failed for %s", file_path)
        return ""


# ────────────────────── TG multi-bot loop ──────────────────────

async def run_agent_poller(bridge: Bridge, cfg: agents_registry.AgentConfig) -> None:
    """
    Запустить отдельного aiogram-бота для одного агента.
    Слушает свой chat_id (или GROUP_CHAT_ID) и свои topic_ids (или topic_id) от любого участника группы.
    Если enabled_in_tg=False — агент не получает TG-сообщений (только через queue).
    """
    if not cfg.enabled_in_tg:
        log.info("[%s] enabled_in_tg=False, skip TG poller", cfg.name)
        return

    token = os.getenv(cfg.bot_token_env)
    if not token:
        log.warning("[%s] no bot token (env %s); skip", cfg.name, cfg.bot_token_env)
        return

    try:
        from aiogram import Bot, Dispatcher, F
        from aiogram.types import Message, CallbackQuery
        from aiogram.client.default import DefaultBotProperties
        from aiogram.client.session.aiohttp import AiohttpSession
        from aiogram.client.telegram import TelegramAPIServer
    except ImportError:
        log.error("aiogram not installed; cannot start poller for %s", cfg.name)
        return

    tg_api_base = os.environ.get("TG_API_BASE", "https://api.telegram.org")
    tg_session = AiohttpSession(api=TelegramAPIServer.from_base(tg_api_base)) if tg_api_base != "https://api.telegram.org" else None
    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=None), session=tg_session)
    dp = Dispatcher()
    bridge.register_bot(cfg.name, bot)
    # Алиасим этот Bot-инстанс всем агентам, которые делят тот же токен
    # (напр. librarian шлёт через бот rajesh). Поллеры им уже не создаём.
    for other_name, other_cfg in agents_registry.AGENTS.items():
        if other_name == cfg.name or not other_cfg.enabled:
            continue
        if os.environ.get(other_cfg.bot_token_env or "", "") == token:
            bridge.register_bot(other_name, bot)
            log.info("[%s] bot aliased from [%s] (shared token)", other_name, cfg.name)

    agent_name = cfg.name
    my_topic = cfg.topic_id
    my_topic_ids = set(cfg.topic_ids) if cfg.topic_ids else None
    my_chat_id = cfg.chat_id if cfg.chat_id else GROUP_CHAT_ID

    # multi-chat — поллер может слушать дополнительные чаты (personal_agents)
    my_chat_ids = {my_chat_id}
    if cfg.extra_chat_ids:
        my_chat_ids.update(cfg.extra_chat_ids)

    # media group buffer — TG шлёт каждый файл в группе отдельным update.
    # Буферизуем по media_group_id, отправляем агенту одним промптом.
    _media_group_buffers: dict[str, list] = {}  # media_group_id -> [message, ...]
    _media_group_timers: dict[str, asyncio.Task] = {}  # media_group_id -> debounce task

    # Маппинг (chat_id, topic_id) → agent для быстрого роутинга в extra-чатах
    _extra_chat_topic_map: dict[tuple[int, int], str] = {}
    for _acfg in agents_registry.enabled_agents():
        _eff_chat = _acfg.chat_id if _acfg.chat_id else GROUP_CHAT_ID
        if _eff_chat in my_chat_ids and _eff_chat != my_chat_id and _acfg.name != agent_name:
            _extra_chat_topic_map[(_eff_chat, _acfg.topic_id)] = _acfg.name

    # shared-token routing — агенты с тем же токеном в primary чате
    _shared_token_topic_map: dict[int, str] = {}  # topic_id → agent_name
    for _acfg in agents_registry.enabled_agents():
        if _acfg.name == agent_name or not _acfg.enabled:
            continue
        _eff_chat = _acfg.chat_id if _acfg.chat_id else GROUP_CHAT_ID
        if _eff_chat == my_chat_id and os.environ.get(_acfg.bot_token_env or "", "") == token:
            _shared_token_topic_map[_acfg.topic_id] = _acfg.name
    if _shared_token_topic_map:
        log.info("[%s] shared-token topics: %s", cfg.name, _shared_token_topic_map)

    def _resolve_agent(msg_chat_id: int, thread_id: int) -> tuple[str, agents_registry.AgentConfig]:
        """Определить агента по (chat_id, topic_id). Fallback = владелец поллера."""
        if msg_chat_id == my_chat_id:
            # check shared-token agents first
            shared_name = _shared_token_topic_map.get(thread_id)
            if shared_name:
                return shared_name, agents_registry.get(shared_name)
            return agent_name, cfg
        target_name = _extra_chat_topic_map.get((msg_chat_id, thread_id))
        if target_name:
            return target_name, agents_registry.get(target_name)
        return agent_name, cfg

    # Получаем id бота для self-message filter
    try:
        bot_me = await bot.get_me()
        bot_user_id = bot_me.id
    except Exception:
        bot_user_id = None

    # Фильтр только по chat_id: бот реагирует на сообщения любого участника
    # своей группы (GROUP_CHAT_ID). Защита от внешних — на уровне chat_id.
    @dp.message(F.chat.id.in_(my_chat_ids))
    async def on_message(message):
        # Self-message filter (предотвращает reply-loop если бот пишет в тот же чат)
        if bot_user_id and message.from_user.id == bot_user_id:
            return
        # Игнорируем других ботов (чтобы агенты не реагировали друг на друга в группе)
        if message.from_user.is_bot:
            return
        # Опциональный allowlist пользователей (если задан ALLOWED_USER_IDS)
        if ALLOWED_USER_IDS and message.from_user.id not in ALLOWED_USER_IDS:
            return

        # Фильтр машинных сообщений (machine-prefix "🤖_<tag>_" в начале)
        raw_text = message.text or message.caption or ""
        if raw_text.startswith("🤖_") or "🤖<i>_" in raw_text[:50]:
            return

        thread = message.message_thread_id or 0
        msg_chat_id = message.chat.id

        # multi-chat routing
        resolved_name, resolved_cfg = _resolve_agent(msg_chat_id, thread)

        # Фильтр по топику: только для primary чата (extra-чаты пропускаем всё)
        if msg_chat_id == my_chat_id:
            # also accept shared-token agent topics
            if thread in _shared_token_topic_map:
                pass  # accepted — will route via _resolve_agent
            elif my_topic_ids is not None:
                if thread not in my_topic_ids:
                    return
            else:
                if thread != my_topic:
                    return

        # media group buffer — если msg часть группы (несколько фото/документов),
        # буферизуем и отправляем одним промптом после debounce 1.5с.
        mg_id = getattr(message, 'media_group_id', None)
        if mg_id:
            if mg_id not in _media_group_buffers:
                _media_group_buffers[mg_id] = []
            _media_group_buffers[mg_id].append(message)

            # Отменяем предыдущий таймер, ставим новый
            old_timer = _media_group_timers.get(mg_id)
            if old_timer and not old_timer.done():
                old_timer.cancel()

            async def _flush_media_group(_mg_id=mg_id, _resolved=resolved_name, _thread=thread):
                await asyncio.sleep(1.5)
                msgs = _media_group_buffers.pop(_mg_id, [])
                _media_group_timers.pop(_mg_id, None)
                if not msgs:
                    return
                # Собираем все файлы из группы
                all_paths = []
                caption_parts = []
                for mg_msg in msgs:
                    cap = mg_msg.caption or ""
                    if cap:
                        caption_parts.append(cap)
                    for attach in _collect_attachments(mg_msg):
                        if len(all_paths) >= MAX_FILES_PER_MESSAGE:
                            log.warning("[%s] media group exceeds %d files — остальные пропущены", _resolved, MAX_FILES_PER_MESSAGE)
                            break
                        try:
                            fp = await _save_attachment(bot, attach, _resolved)
                            if fp:
                                all_paths.append(fp)
                        except Exception:
                            log.exception("[%s] media group file download failed", _resolved)
                text_parts = []
                if caption_parts:
                    text_parts.append("\n".join(dict.fromkeys(caption_parts)))  # dedupe captions
                if all_paths:
                    text_parts.append("\n[Прикреплённые файлы (группа). Текст/код/картинки — Read tool; pdf/xlsx/pptx — через навык или python:]")
                    for fp in all_paths:
                        text_parts.append(f"  - {fp}")
                final_text = "\n".join(text_parts).strip()
                if final_text:
                    log.info("[%s] media group %s: %d files merged", _resolved, _mg_id, len(all_paths))
                    await bridge.deliver_user_message(_resolved, final_text, topic_id=_thread)

            _media_group_timers[mg_id] = asyncio.create_task(_flush_media_group())
            return  # не обрабатываем дальше — flush отправит

        parts: list[str] = []

        # ─────── Reply context ───────
        # Если пользователь отвечает реплаем на конкретное сообщение —
        # подтягиваем текст этого сообщения как контекст для агента.
        if message.reply_to_message:
            reply_msg = message.reply_to_message
            reply_text = reply_msg.text or reply_msg.caption or ""
            if reply_text:
                # Strip machine prefix (watchdog/reporter posts)
                if reply_text.startswith("\U0001f916_") or "\U0001f916<i>_" in reply_text[:50]:
                    reply_text = "\n".join(reply_text.split("\n")[1:]).strip()
                if len(reply_text) > 800:
                    reply_text = reply_text[:800] + "\u2026"
                is_from_bot = reply_msg.from_user and (
                    bot_user_id and reply_msg.from_user.id == bot_user_id
                )
                sender = cfg.display_name if is_from_bot else "Пользователь"
                # Detect old session: reply to a message sent before last session reset
                session_hint = ""
                try:
                    st = bridge.sessions.load(agent_name)
                    if st.last_reset and reply_msg.date:
                        reset_dt = datetime.fromisoformat(st.last_reset)
                        reply_dt = reply_msg.date.replace(tzinfo=timezone.utc) if reply_msg.date.tzinfo is None else reply_msg.date
                        if reply_dt < reset_dt:
                            session_hint = " (из прошлой сессии — у тебя нет этого контекста, используй цитату)"
                except Exception:
                    pass
                parts.append(f"[Реплай на сообщение {sender}{session_hint}:]\n\xab{reply_text}\xbb\n")

        if message.text:
            parts.append(message.text)
        if message.caption:
            parts.append(message.caption)

        # Прикреплённые файлы — скачиваем + разделяем voice и остальные
        saved: list[tuple[str, str]] = []  # (kind, path)
        has_files = False
        try:
            for attach in _collect_attachments(message):
                fp = await _save_attachment(bot, attach, agent_name)
                if fp:
                    saved.append((attach[0], fp))
                    has_files = True
        except Exception:
            log.exception("[%s] file download failed", agent_name)

        # Voice/audio/video_note → транскрибируем с прогресс-статусом в TG
        other_paths: list[str] = []
        status_msg = None
        for kind, fp in saved:
            if kind in ("voice", "audio", "video_note"):
                try:
                    # Статус: получено
                    try:
                        status_msg = await bot.send_message(
                            my_chat_id,
                            "🎤 Voice получен, отправляю на транскрибацию...",
                            message_thread_id=thread,
                        )
                    except Exception:
                        pass

                    log.info("[%s] transcribing %s: %s", agent_name, kind, fp)
                    transcription = await _transcribe_voice(fp)

                    if transcription:
                        parts.append(f"[Голосовое сообщение (транскрипция):] {transcription}")
                        log.info("[%s] voice transcribed: %d chars", agent_name, len(transcription))
                        # Статус: транскрибировано, агент обрабатывает
                        if status_msg:
                            try:
                                preview = transcription[:100] + ("..." if len(transcription) > 100 else "")
                                await bot.edit_message_text(
                                    f"🎤 Транскрибировано:\n\n«{preview}»\n\n⏳ {cfg.display_name} обрабатывает запрос...",
                                    chat_id=my_chat_id,
                                    message_id=status_msg.message_id,
                                )
                            except Exception:
                                pass
                    else:
                        parts.append(f"[Голосовое — транскрипция не удалась, файл: {fp}]")
                        if status_msg:
                            try:
                                await bot.edit_message_text(
                                    "❌ Транскрипция не удалась, передаю файл агенту",
                                    chat_id=my_chat_id,
                                    message_id=status_msg.message_id,
                                )
                            except Exception:
                                pass
                except Exception:
                    log.exception("[%s] voice transcription failed", agent_name)
                    parts.append(f"[Голосовое сообщение, файл: {fp}]")
            else:
                other_paths.append(fp)

        if other_paths:
            parts.append("\n[Прикреплённые файлы. Текст/код/картинки — Read tool; pdf/xlsx/pptx — через навык или python:]")
            for fp in other_paths:
                parts.append(f"  - {fp}")

        text = "\n".join(parts).strip()
        if not text:
            return

        # ─────── Slash-команды моста ───────
        # Обрабатываются локально ботом, НЕ уходят в runner (0 токенов).
        # Неизвестные "/xxx" проваливаются в runner как обычный текст —
        # вдруг это вызов скилла.
        # используем resolved_name/resolved_cfg для multi-chat routing
        if not has_files and text.startswith("/"):
            cmd_parts = text.split(None, 1)
            cmd = cmd_parts[0].split("@", 1)[0].lower()

            if cmd == "/new":
                from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
                st_cur = bridge.sessions.load(resolved_name)
                sid_short = (st_cur.session_id[:8] + "…") if st_cur.session_id else "—"
                kb = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="✅ Да, ресет", callback_data=f"new:yes:{resolved_name}"),
                    InlineKeyboardButton(text="❌ Отмена",    callback_data=f"new:no:{resolved_name}"),
                ]])
                warn = (
                    f"🔄 Сбросить сессию агента {resolved_cfg.display_name} ({resolved_name})?\n\n"
                    f"state: {st_cur.state.value}\n"
                    f"session_id: {sid_short}\n"
                    f"turns: {st_cur.turns}\n\n"
                    f"После сброса следующий turn пойдёт cold start: "
                    f"агент перечитает CLAUDE.md и context.md."
                )
                try:
                    await bot.send_message(
                        msg_chat_id, warn,
                        message_thread_id=thread,
                        reply_markup=kb,
                    )
                except Exception:
                    log.exception("[%s] /new prompt send failed", resolved_name)
                return

            if cmd == "/state":
                st_cur = bridge.sessions.load(resolved_name)
                msg_text = (
                    f"📊 {resolved_cfg.display_name} ({resolved_name})\n"
                    f"state:         {st_cur.state.value}\n"
                    f"session_id:    {st_cur.session_id or '—'}\n"
                    f"turns:         {st_cur.turns}\n"
                    f"last_activity: {st_cur.last_activity or '—'}\n"
                    f"waiting_for:   {', '.join(st_cur.waiting_for) if st_cur.waiting_for else '—'}\n"
                    f"last_reset:    {st_cur.last_reset or '—'}\n"
                    f"last_compact:  {st_cur.last_compact or '—'}"
                )
                try:
                    await bot.send_message(
                        msg_chat_id, msg_text,
                        message_thread_id=thread,
                    )
                except Exception:
                    log.exception("[%s] /state send failed", resolved_name)
                return

            if cmd == "/help":
                help_text = (
                    "🤖 Команды моста (локальные, 0 токенов):\n\n"
                    "/new — сбросить сессию агента. Следующее сообщение начнёт "
                    "чистую сессию (cold start — агент перечитает CLAUDE.md + "
                    "context.md). Требует подтверждения через inline-кнопки.\n\n"
                    "/state — показать текущее состояние агента: state, "
                    "session_id, turns, last_activity, waiting_for.\n\n"
                    "/help — эта справка.\n\n"
                    "Любое другое сообщение — обычный ввод для агента."
                )
                try:
                    await bot.send_message(
                        msg_chat_id, help_text,
                        message_thread_id=thread,
                    )
                except Exception:
                    log.exception("[%s] /help send failed", resolved_name)
                return

        # forward detection — пересланные сообщения получают is_forward=True
        # для forward coalescing в worker (склейка нескольких forward'ов в один промпт)
        _is_fwd = bool(message.forward_origin or message.forward_date)
        if _is_fwd:
            text = f"[Пересланное сообщение:]\n{text}"

        log.info("[%s→%s] msg chat=%s topic=%s fwd=%s: %r (files=%d)",
                 agent_name, resolved_name, msg_chat_id, thread, _is_fwd, text[:100], len(saved))
        await bridge.deliver_user_message(resolved_name, text, topic_id=thread, is_forward=_is_fwd)

    @dp.callback_query(F.data.startswith("appr:"))
    async def on_approval_callback(callback: CallbackQuery):
        """Обработка нажатия ✅/❌ кнопок апрува. Жать может любой участник группы (или allowlist)."""
        if ALLOWED_USER_IDS and callback.from_user.id not in ALLOWED_USER_IDS:
            await callback.answer("Не твоя кнопка 🙂", show_alert=False)
            return
        parts = callback.data.split(":", 2)
        if len(parts) != 3:
            await callback.answer()
            return
        _, verdict, key = parts
        approved = (verdict == "yes")
        await callback.answer("✅ Принято" if approved else "❌ Отклонено")
        await bridge.handle_approval_callback(key, approved, callback)

    @dp.callback_query(F.data.startswith("new:"))
    async def on_new_callback(callback: CallbackQuery):
        """Обработка подтверждения /new (сброс сессии). Жать может любой участник группы (или allowlist)."""
        if ALLOWED_USER_IDS and callback.from_user.id not in ALLOWED_USER_IDS:
            await callback.answer("Не твоя кнопка 🙂", show_alert=False)
            return
        parts = callback.data.split(":", 2)
        if len(parts) != 3:
            await callback.answer()
            return
        _, verdict, target_agent = parts

        if verdict == "no":
            await callback.answer("Отменено")
            try:
                await callback.message.edit_text(
                    f"❌ Сброс сессии {target_agent} отменён."
                )
            except Exception:
                pass
            return

        # yes — ресетим
        st_tgt = bridge.sessions.load(target_agent)
        if st_tgt.state == State.BUSY:
            await callback.answer(
                "Агент сейчас BUSY (идёт tool call). Подожди и повтори.",
                show_alert=True,
            )
            return

        # session summary для personal-агентов перед reset
        if target_agent.startswith("personal_") and st_tgt.session_id:
            try:
                tgt_cfg = agents_registry.get(target_agent)
                eff_chat = tgt_cfg.chat_id if tgt_cfg.chat_id else GROUP_CHAT_ID
                await callback.answer("⏳ Записываю саммари сессии...")
                try:
                    await callback.message.edit_text(
                        f"⏳ {target_agent}: записываю саммари сессии перед сбросом..."
                    )
                except Exception:
                    pass
                await bridge.deliver_user_message(
                    target_agent,
                    "[SESSION_SUMMARY] Сессия завершается по /new. "
                    "Обнови JSON-хранилища из workspace/personal/ — "
                    "запиши всё из этой сессии что ещё не записано. "
                    "Ответь коротким саммари (2-3 предложения).",
                    topic_id=tgt_cfg.topic_id,
                )
                # Даём агенту 60 секунд на ответ
                for _ in range(60):  # 120 sec timeout for session summary
                    await asyncio.sleep(2)
                    st_check = bridge.sessions.load(target_agent)
                    if st_check.state != State.BUSY:
                        break
                log.info("[%s] session summary completed before /new reset", target_agent)
            except Exception:
                log.exception("[%s] session summary before /new failed", target_agent)

        bridge.sessions.on_reset_to_idle(target_agent)

        await callback.answer("✅ Сессия сброшена")
        try:
            await callback.message.edit_text(
                f"🔄 Сессия {target_agent} сброшена (state → IDLE, session_id → None).\n"
                f"Следующее сообщение пойдёт cold start."
            )
        except Exception:
            pass
        log.info("[%s] session reset via /new command by user %s",
                 target_agent, callback.from_user.id)

    # drain backlog — вычитать все pending updates без обработки.
    # Предотвращает шторм запусков после рестарта bridge.
    try:
        drained = await bot.get_updates(offset=-1, timeout=0)
        if drained:
            last_id = drained[-1].update_id
            await bot.get_updates(offset=last_id + 1, timeout=0)
            log.info("[%s] drained %d backlog updates on startup", cfg.name, len(drained))
    except Exception:
        log.warning("[%s] backlog drain failed (non-fatal)", cfg.name)

    log.info("[%s] poller starting (topic=%s token_env=%s)", cfg.name, my_topic, cfg.bot_token_env)
    try:
        await dp.start_polling(
            bot,
            handle_signals=False,
            allowed_updates=["message", "callback_query", "channel_post", "message_reaction_count"],
        )
    except Exception:
        log.exception("[%s] poller crashed", cfg.name)
    finally:
        try:
            await bot.session.close()
        except Exception:
            pass


# ────────────────────── main ──────────────────────

async def main() -> None:
    bridge = Bridge()

    log.info("bridge v%s starting; enabled agents: %s", BRIDGE_VERSION,
             ", ".join(a.name for a in agents_registry.enabled_agents()))

    tasks = [asyncio.create_task(bridge.maintenance_loop(), name="maintenance")]
    # Стартовый welcome + self-check в топик Сисадмина (ждёт регистрацию бота внутри).
    tasks.append(asyncio.create_task(bridge.startup_announce(), name="startup-announce"))
    # Если был self-restart — вернуться к инициировавшему агенту и резюмить его сессию.
    tasks.append(asyncio.create_task(bridge.restart_resume(), name="restart-resume"))
    # Несколько агентов могут делить один bot token (напр. все 3 агента →
    # BOT_TOKEN). На один токен допустим только один getUpdates-поллер —
    # иначе Telegram отвечает TelegramConflictError на оба. Для «вторичных»
    # агентов поллер пропускаем: run_agent_poller первого зарегистрирует один
    # Bot-инстанс под все имена агентов с этим токеном (см. register_bot alias).
    seen_tokens: set[str] = set()
    for cfg in agents_registry.enabled_agents():
        token = os.environ.get(cfg.bot_token_env or "", "")
        if token and token in seen_tokens:
            log.info("[%s] shares bot token with earlier agent — poller skipped", cfg.name)
            continue
        if token:
            seen_tokens.add(token)
        tasks.append(asyncio.create_task(run_agent_poller(bridge, cfg), name=f"poller-{cfg.name}"))

    shutdown_started = False

    def request_shutdown() -> None:
        nonlocal shutdown_started
        if shutdown_started:
            return
        shutdown_started = True
        log.info("shutdown requested")
        bridge._shutdown.set()
        for task in tasks:
            task.cancel()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, request_shutdown)
        except NotImplementedError:
            pass

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await bridge.shutdown()
    log.info("bridge stopped")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
