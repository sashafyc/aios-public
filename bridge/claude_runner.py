"""
claude_runner.py — запуск Claude Code CLI как раннера агента.

Две реализации общего интерфейса AgentRunner:

  1. SubprocessRunner — на каждое сообщение вызывает `claude -p … --resume`.
     Простой, надёжный старт. Кэш жив пока cacheRetention=long (до 1 часа).
     Поддерживает streaming через run_streaming() (stream-json + partial messages).

  2. PTYRunner — держит long-running процесс через pexpect + pseudo-terminal.
     Cache hit ~90%. Сложнее в реализации, используется как фаза 2 оптимизации.
     На старте оставлен как скелет с NotImplementedError в hot-path'ах.

Почему subprocess: `claude -p --resume` — простой и официально поддерживаемый
способ программно использовать Claude CLI (включая подписку), без отдельного SDK.

Входы: name, workdir, message
Выходы: RunResult(text, usage, session_id, duration_ms)
Используют: tg_bridge, trigger.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncGenerator, Optional


log = logging.getLogger("bridge.claude_runner")


# Путь к settings.json (permissions/blockedCommands) — рядом с этим файлом в bridge/.
SETTINGS_PATH = str(Path(__file__).resolve().parent / "settings.json")


@dataclass
class RunResult:
    text: str                             # финальный ответ агента (то что бы увидел юзер)
    usage: dict = field(default_factory=dict)  # tokens_in/out/cache_read/cache_write
    session_id: Optional[str] = None
    cost_usd: float = 0.0
    duration_ms: int = 0
    error: Optional[str] = None
    is_truncated: bool = False            # v9.9: True если ответ обрезан по output limit


@dataclass
class StreamEvent:
    """Событие потокового вывода Claude CLI (stream-json)."""
    kind: str       # "text_delta" | "tool_start" | "tool_end" | "result" | "error"
    text: str = ""
    tool_name: str = ""
    tool_id: str = ""
    result: Optional[RunResult] = None


class AgentRunner(ABC):
    """Общий интерфейс. Реализации: SubprocessRunner, PTYRunner."""

    @abstractmethod
    async def run(
        self,
        message: str,
        *,
        resume: bool = True,
        session_id: Optional[str] = None,
    ) -> RunResult:
        ...

    @abstractmethod
    async def run_isolated(self, message: str, *, session_id: Optional[str] = None) -> RunResult:
        """
        Одноразовый запуск без --resume и без наследования session_id.
        Результат НЕ мутирует внутреннее состояние раннера (_last_session_id).
        Используется для cron-триггеров: каждый запуск стартует с нуля и не
        загрязняет main-сессию агента.
        Если session_id задан — добавляет --resume (экономия кэша между запусками).
        """

    @abstractmethod
    async def compact(self, *, session_id: Optional[str] = None) -> None:
        """Эквивалент /compact — сжать контекст."""

    @abstractmethod
    async def reset(self) -> None:
        """Эквивалент /new — killed → начнётся новая сессия при следующем run()."""

    @abstractmethod
    async def close(self) -> None:
        """Освободить ресурсы (для PTY). No-op для subprocess."""


# v9.9: iteration budget — максимальное число tool calls в одном run.
# Предотвращает runaway agents (бесконечные циклы web_search → fail → retry).
# Default: 90 для обычных, 50 для isolated (cron). Env-override: TOOL_BUDGET.
TOOL_BUDGET = int(os.getenv("TOOL_BUDGET", "90"))
TOOL_BUDGET_ISOLATED = int(os.getenv("TOOL_BUDGET_ISOLATED", "50"))


# ─────────────────────────────── Subprocess ───────────────────────────────


class SubprocessRunner(AgentRunner):
    """
    Запускает `claude -p <msg> --resume --output-format json` как subprocess,
    парсит JSON, возвращает RunResult.

    Идемпотентен: несколько параллельных run() на одной workdir безопасны только
    если верхний слой их сериализует (bridge держит lock per-agent).
    """

    def __init__(
        self,
        name: str,
        workdir: Path,
        *,
        model: Optional[str] = None,
        settings_path: str = SETTINGS_PATH,
        timeout_s: int = 600,
        run_as_user: Optional[str] = None,
    ):
        self.name = name
        self.workdir = Path(workdir)
        self.model = model
        self.settings_path = settings_path
        self.timeout_s = timeout_s
        self.run_as_user = run_as_user
        self._last_session_id: Optional[str] = None

    def _wrap_cmd(self, cmd: list[str]) -> list[str]:
        """Wrap command with sudo if run_as_user is set."""
        if self.run_as_user:
            return ["sudo", "-u", self.run_as_user, "--"] + cmd
        return cmd

    @staticmethod
    def _sanitize_prompt(message: str) -> str:
        """
        v9.8: защита от edge cases когда CLI интерпретирует промпт как флаг.
        - Начинается с '-' → добавить пробел (не будет считаться аргументом).
        - Чисто числовой → добавить точку (некоторые CLI интерпретируют как ID).
        """
        if not message:
            return message
        if message.lstrip().startswith("-"):
            message = " " + message
        if message.strip().isdigit():
            message = message.strip() + "."
        return message

    async def run(
        self,
        message: str,
        *,
        resume: bool = True,
        session_id: Optional[str] = None,
    ) -> RunResult:
        message = self._sanitize_prompt(message)
        sid = session_id or self._last_session_id
        cmd = [
            "claude",
            "-p", message,
            "--output-format", "json",
            "--dangerously-skip-permissions",
            "--settings", self.settings_path,
        ]
        if resume and sid:
            cmd += ["--resume", sid]
        if self.model:
            cmd += ["--model", self.model]

        cmd = self._wrap_cmd(cmd)
        log.debug("[%s] spawn: %s cwd=%s", self.name, shlex.join(cmd), self.workdir)

        started = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(self.workdir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=10 * 1024 * 1024,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.timeout_s)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return RunResult(text="", error=f"timeout after {self.timeout_s}s")
        except FileNotFoundError:
            return RunResult(text="", error="`claude` binary not found in PATH")
        except Exception as exc:
            log.exception("[%s] subprocess crashed", self.name)
            return RunResult(text="", error=f"spawn failed: {exc}")

        duration_ms = int((time.monotonic() - started) * 1000)

        if proc.returncode != 0:
            err = (stderr or b"").decode("utf-8", errors="replace")[:2000]
            return RunResult(text="", error=f"claude exited {proc.returncode}: {err}", duration_ms=duration_ms)

        raw = (stdout or b"").decode("utf-8", errors="replace")
        return self._parse_json_output(raw, duration_ms)

    # ─────────────── streaming ───────────────

    async def run_streaming(
        self,
        message: str,
        *,
        resume: bool = True,
        session_id: Optional[str] = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """
        Потоковый запуск: yield StreamEvent по мере генерации.
        Последний event имеет kind="result" с финальным RunResult.

        Использует: --output-format stream-json --verbose --include-partial-messages.
        """
        message = self._sanitize_prompt(message)
        sid = session_id or self._last_session_id
        cmd = [
            "claude",
            "-p", message,
            "--output-format", "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--dangerously-skip-permissions",
            "--settings", self.settings_path,
        ]
        if resume and sid:
            cmd += ["--resume", sid]
        if self.model:
            cmd += ["--model", self.model]

        cmd = self._wrap_cmd(cmd)
        log.debug("[%s] stream spawn: %s cwd=%s", self.name, shlex.join(cmd), self.workdir)

        started = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(self.workdir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=10 * 1024 * 1024,
            )
        except FileNotFoundError:
            yield StreamEvent(kind="error", text="`claude` binary not found")
            return
        except Exception as exc:
            log.exception("[%s] stream spawn failed", self.name)
            yield StreamEvent(kind="error", text=f"spawn failed: {exc}")
            return

        got_result = False
        try:
            async for event in self._parse_stream(proc, started):
                if event.kind == "result":
                    got_result = True
                yield event
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            duration_ms = int((time.monotonic() - started) * 1000)
            yield StreamEvent(
                kind="result",
                result=RunResult(text="", error=f"timeout after {self.timeout_s}s", duration_ms=duration_ms),
            )
            return
        except Exception as exc:
            log.exception("[%s] streaming error", self.name)
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            if not got_result:
                duration_ms = int((time.monotonic() - started) * 1000)
                yield StreamEvent(
                    kind="result",
                    result=RunResult(text="", error=str(exc), duration_ms=duration_ms),
                )

    async def _parse_stream(
        self,
        proc: asyncio.subprocess.Process,
        started: float,
        *,
        tool_budget: int = 0,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Парсинг NDJSON stdout от claude --output-format stream-json."""
        deadline = started + self.timeout_s
        budget = tool_budget or TOOL_BUDGET
        tool_count = 0

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise asyncio.TimeoutError()

            try:
                line = await asyncio.wait_for(
                    proc.stdout.readline(),
                    timeout=min(remaining, 30),
                )
            except asyncio.TimeoutError:
                # readline timeout — проверяем жив ли процесс
                if proc.returncode is not None:
                    break
                continue

            if not line:
                # EOF — процесс закрылся
                break

            line_str = line.decode("utf-8", errors="replace").strip()
            if not line_str:
                continue

            try:
                data = json.loads(line_str)
            except json.JSONDecodeError:
                continue

            evt_type = data.get("type", "")

            if evt_type == "stream_event":
                event = data.get("event", {})
                inner_type = event.get("type", "")

                if inner_type == "content_block_delta":
                    delta = event.get("delta", {})
                    if delta.get("type") == "text_delta":
                        text = delta.get("text", "")
                        if text:
                            yield StreamEvent(kind="text_delta", text=text)

                elif inner_type == "content_block_start":
                    block = event.get("content_block", {})
                    if block.get("type") == "tool_use":
                        tool_count += 1
                        tool_name = block.get("name", "unknown")
                        tool_id = block.get("id", "")
                        # v9.9: iteration budget — kill process if too many tool calls
                        if tool_count > budget:
                            log.warning(
                                "[%s] tool budget exhausted (%d/%d), killing process",
                                self.name, tool_count, budget,
                            )
                            proc.kill()
                            await proc.wait()
                            duration_ms = int((time.monotonic() - started) * 1000)
                            yield StreamEvent(
                                kind="result",
                                result=RunResult(
                                    text=f"⚠️ Достигнут лимит tool calls ({budget}). "
                                         f"Агент остановлен для защиты от зацикливания.",
                                    duration_ms=duration_ms,
                                    error=f"tool budget exhausted ({tool_count}/{budget})",
                                ),
                            )
                            return
                        yield StreamEvent(
                            kind="tool_start",
                            tool_name=tool_name,
                            tool_id=tool_id,
                        )

            elif evt_type == "result":
                duration_ms = int((time.monotonic() - started) * 1000)
                text = data.get("result", "")
                usage = data.get("usage", {})
                session_id = data.get("session_id")
                cost = float(data.get("total_cost_usd", 0.0))

                if session_id:
                    self._last_session_id = session_id

                is_error = data.get("is_error", False)
                error = None
                if is_error:
                    error = text or "unknown error"
                    text = ""
                stop_reason = data.get("stop_reason") or data.get("finish_reason") or ""
                truncated = stop_reason == "max_tokens" or bool(data.get("is_truncated"))

                yield StreamEvent(
                    kind="result",
                    result=RunResult(
                        text=text,
                        usage=usage,
                        session_id=session_id,
                        cost_usd=cost,
                        duration_ms=duration_ms,
                        error=error,
                        is_truncated=truncated,
                    ),
                )
                break

        # Ждём завершения процесса
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()

    # ─────────────── isolated ───────────────

    async def run_isolated(self, message: str, *, session_id: Optional[str] = None) -> RunResult:
        """
        Одноразовый прогон без --resume и без обновления _last_session_id.
        Ничего не наследует и ничего не сохраняет — идеально для cron-ов.
        Если session_id задан — добавляет --resume <session_id> для продолжения сессии.
        """
        message = self._sanitize_prompt(message)
        cmd = [
            "claude",
            "-p", message,
            "--output-format", "json",
            "--dangerously-skip-permissions",
            "--settings", self.settings_path,
        ]
        if session_id:
            cmd += ["--resume", session_id]
        if self.model:
            cmd += ["--model", self.model]

        cmd = self._wrap_cmd(cmd)
        log.debug("[%s] spawn ISOLATED: %s cwd=%s", self.name, shlex.join(cmd), self.workdir)

        started = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(self.workdir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=10 * 1024 * 1024,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.timeout_s)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return RunResult(text="", error=f"timeout after {self.timeout_s}s")
        except FileNotFoundError:
            return RunResult(text="", error="`claude` binary not found in PATH")
        except Exception as exc:
            log.exception("[%s] isolated subprocess crashed", self.name)
            return RunResult(text="", error=f"spawn failed: {exc}")

        duration_ms = int((time.monotonic() - started) * 1000)

        if proc.returncode != 0:
            err = (stderr or b"").decode("utf-8", errors="replace")[:2000]
            return RunResult(text="", error=f"claude exited {proc.returncode}: {err}", duration_ms=duration_ms)

        raw = (stdout or b"").decode("utf-8", errors="replace")
        return self._parse_json_output_isolated(raw, duration_ms)

    def _parse_json_output_isolated(self, raw: str, duration_ms: int) -> RunResult:
        """
        Как _parse_json_output, но НЕ обновляет self._last_session_id.
        Изолированный запуск не должен влиять на main-сессию.
        """
        raw = raw.strip()
        if not raw:
            return RunResult(text="", error="empty stdout", duration_ms=duration_ms)

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            tail = raw.strip().splitlines()[-1]
            try:
                data = json.loads(tail)
            except json.JSONDecodeError:
                return RunResult(text=raw[:4000], error="non-json output", duration_ms=duration_ms)

        text = data.get("result") or data.get("text") or data.get("output") or ""
        usage = data.get("usage") or {}
        session_id = data.get("session_id") or data.get("id")
        cost = float(data.get("cost_usd") or data.get("total_cost_usd") or 0.0)
        stop_reason = data.get("stop_reason") or data.get("finish_reason") or ""
        truncated = stop_reason == "max_tokens" or bool(data.get("is_truncated"))

        # НЕ обновляем self._last_session_id — в этом вся суть isolated.
        return RunResult(
            text=text,
            usage=usage,
            session_id=session_id,
            cost_usd=cost,
            duration_ms=duration_ms,
            is_truncated=truncated,
        )

    def _parse_json_output(self, raw: str, duration_ms: int) -> RunResult:
        """
        `claude -p --output-format json` возвращает объект с полями:
            { "result": "...", "session_id": "...", "usage": {...}, "cost_usd": ... }
        Формат может отличаться между версиями CLI — парсим толерантно.
        """
        raw = raw.strip()
        if not raw:
            return RunResult(text="", error="empty stdout", duration_ms=duration_ms)

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            # иногда CLI печатает несколько JSON-объектов (stream-json-ish) — возьмём последний
            tail = raw.strip().splitlines()[-1]
            try:
                data = json.loads(tail)
            except json.JSONDecodeError:
                return RunResult(text=raw[:4000], error="non-json output", duration_ms=duration_ms)

        text = data.get("result") or data.get("text") or data.get("output") or ""
        usage = data.get("usage") or {}
        session_id = data.get("session_id") or data.get("id")
        cost = float(data.get("cost_usd") or data.get("total_cost_usd") or 0.0)
        # v9.9: detect truncation (stop_reason=max_tokens or explicit is_truncated)
        stop_reason = data.get("stop_reason") or data.get("finish_reason") or ""
        truncated = stop_reason == "max_tokens" or bool(data.get("is_truncated"))

        if session_id:
            self._last_session_id = session_id

        return RunResult(
            text=text,
            usage=usage,
            session_id=session_id,
            cost_usd=cost,
            duration_ms=duration_ms,
            is_truncated=truncated,
        )

    async def compact(self, *, session_id: Optional[str] = None) -> None:
        """
        Subprocess-режим: отправка спец-инструкции как обычного сообщения.
        session_id передаётся из .state чтобы compact шёл в реальную сессию,
        а не в orphan (без --resume).
        """
        await self.run(
            "Пожалуйста, сожми контекст нашего диалога в 1-2 абзаца "
            "ключевого состояния и отбрось старые детали. Это внутренняя "
            "операция, не упоминай её в публичных сообщениях.",
            session_id=session_id,
        )

    async def reset(self) -> None:
        """
        В subprocess-модели сессия переиспользуется через --resume. Reset =
        следующий run() без --resume. Конкретная реализация зависит от того,
        как bridge хранит 'следует ли resume'. Здесь мы просто забываем
        последний session_id; решение о resume=False делает bridge.
        """
        self._last_session_id = None

    async def close(self) -> None:
        pass


# ─────────────────────────────── PTY (фаза 2) ───────────────────────────────


class PTYRunner(AgentRunner):
    """
    Long-running процесс `claude` через pexpect + pseudo-terminal.
    Cache hit ~90%, conversation history в памяти процесса.

    СТАТУС: скелет. На старте bridge использует SubprocessRunner.
    Реализация ― фаза оптимизации после снятия метрик.
    """

    def __init__(self, name: str, workdir: Path, *, model: Optional[str] = None):
        self.name = name
        self.workdir = Path(workdir)
        self.model = model
        self._child = None  # pexpect.spawn

    async def _ensure_started(self) -> None:
        if self._child is not None:
            return
        try:
            import pexpect  # noqa: F401
        except ImportError as exc:
            raise RuntimeError("PTYRunner требует pexpect (pip install pexpect)") from exc
        # TODO(phase2): pexpect.spawn("claude --output-format stream-json", cwd=self.workdir)
        # + read_until_turn_end parser + sync по prompt-маркеру.
        raise NotImplementedError("PTYRunner is phase-2; use SubprocessRunner on start")

    async def run(
        self,
        message: str,
        *,
        resume: bool = True,
        session_id: Optional[str] = None,
    ) -> RunResult:
        await self._ensure_started()
        raise NotImplementedError

    async def run_isolated(self, message: str) -> RunResult:
        # В PTY-модели isolated бессмыслен (весь PTY — это переиспользование
        # процесса). Для cron-триггеров используем SubprocessRunner.run_isolated.
        raise NotImplementedError("use SubprocessRunner.run_isolated for cron triggers")

    async def compact(self, *, session_id: Optional[str] = None) -> None:
        raise NotImplementedError

    async def reset(self) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        if self._child is not None:
            try:
                self._child.close(force=True)
            except Exception:
                pass
            self._child = None


# ─────────────────────────────── factory ───────────────────────────────


def make_runner(
    kind: str,
    name: str,
    workdir: Path,
    *,
    model: Optional[str] = None,
) -> AgentRunner:
    """
    Фабрика ― возвращает нужную реализацию по конфигу.
        kind ∈ {"subprocess", "pty"}
    """
    kind = kind.lower()
    if kind == "subprocess":
        return SubprocessRunner(name, workdir, model=model)
    if kind == "pty":
        return PTYRunner(name, workdir, model=model)
    raise ValueError(f"unknown runner kind: {kind!r}")
