"""
codex_runner.py — раннер для Codex/GPT CLI (OpenAI).

Интерфейс тот же, что у claude_runner.AgentRunner, чтобы bridge мог
работать с разными runner'ами единообразно.

Codex CLI: `codex -p <msg> --resume --output-format json` (формат
предполагается аналогичным claude; при несоответствии парсер даст fallback).

Входы: name, workdir, message
Выходы: RunResult
Используют: tg_bridge, trigger.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Optional

from claude_runner import AgentRunner, RunResult, SubprocessRunner
import pricing as _pricing


log = logging.getLogger("bridge.codex_runner")


class CodexSubprocessRunner(AgentRunner):
    """
    Тонкая обёртка над `codex` CLI по аналогии с SubprocessRunner.
    Если CLI чего-то не поддерживает ― fallback на минимальный набор флагов.
    """

    def __init__(
        self,
        name: str,
        workdir: Path,
        *,
        model: Optional[str] = None,
        timeout_s: int = 600,
    ):
        self.name = name
        self.workdir = Path(workdir)
        self.model = model or "gpt-5.4-codex"
        self.timeout_s = timeout_s
        self._last_session_id: Optional[str] = None

    async def run(self, message: str, *, resume: bool = True, session_id: Optional[str] = None) -> RunResult:
        message = SubprocessRunner._sanitize_prompt(message)
        sid = session_id or self._last_session_id
        if resume and sid:
            # `codex exec resume <sid> <prompt>` — НЕ поддерживает -C, используем cwd.
            cmd = [
                "codex", "exec", "resume",
                "--json",
                "--skip-git-repo-check",
                "--dangerously-bypass-approvals-and-sandbox",
            ]
            if self.model:
                cmd += ["-m", self.model]
            cmd += [sid, message]
        else:
            cmd = [
                "codex", "exec",
                "--json",
                "--skip-git-repo-check",
                "--dangerously-bypass-approvals-and-sandbox",
                "-C", str(self.workdir),
            ]
            if self.model:
                cmd += ["-m", self.model]
            cmd.append(message)

        log.debug("[%s] codex spawn: resume=%s sid=%s", self.name, bool(resume and sid), (sid or "new")[:8])

        started = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(self.workdir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=self.timeout_s
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return RunResult(text="", error=f"codex timeout {self.timeout_s}s")
        except FileNotFoundError:
            return RunResult(text="", error="`codex` binary not found in PATH")
        except Exception as exc:
            log.exception("[%s] codex subprocess crashed", self.name)
            return RunResult(text="", error=f"spawn failed: {exc}")

        duration_ms = int((time.monotonic() - started) * 1000)

        if proc.returncode != 0:
            err = (stderr or b"").decode("utf-8", errors="replace")[:2000]
            # Если resume сломался — fallback на новую сессию (без --resume).
            if resume and sid and ("not found" in err.lower() or "invalid" in err.lower()):
                log.warning("[%s] resume failed (stale sid=%s), retrying new session", self.name, sid[:8])
                self._last_session_id = None
                return await self.run(message, resume=False)
            return RunResult(
                text="", error=f"codex exit {proc.returncode}: {err}", duration_ms=duration_ms
            )

        raw = (stdout or b"").decode("utf-8", errors="replace").strip()
        if not raw:
            return RunResult(text="", error="empty stdout", duration_ms=duration_ms)

        return self._parse_jsonl_output(raw, duration_ms)

    def _parse_jsonl_output(self, raw: str, duration_ms: int) -> RunResult:
        """
        Parse JSONL output from `codex exec --json`.
        Events: thread.started, item.completed, turn.completed.
        """
        thread_id = None
        text_parts = []
        usage = {}
        for line in raw.strip().splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            ev_type = ev.get("type", "")
            if ev_type == "thread.started":
                thread_id = ev.get("thread_id")
            elif ev_type == "item.completed":
                item = ev.get("item", {})
                if item.get("type") == "agent_message" and item.get("text"):
                    text_parts.append(item["text"])
            elif ev_type == "turn.completed":
                usage = ev.get("usage", {})
            elif ev_type == "turn.failed":
                err = ev.get("error", {}).get("message", "unknown error")
                return RunResult(text="", error=f"codex turn failed: {err}", duration_ms=duration_ms)

        text = "\n".join(text_parts)
        if thread_id:
            self._last_session_id = thread_id

        _ti = int(usage.get("input_tokens") or usage.get("input") or 0)
        _to = int(usage.get("output_tokens") or usage.get("output") or 0)
        return RunResult(
            text=text[:16000],
            usage=usage,
            session_id=thread_id,
            cost_usd=_pricing.cost_usd(self.model, _ti, _to),
            duration_ms=duration_ms,
        )

    async def run_isolated(self, message: str) -> RunResult:
        """
        Одноразовый прогон через `codex exec --ephemeral --json`.
        Без сохранения сессии, без --resume, без обновления _last_session_id.
        Используется для cron-триггеров.
        """
        message = SubprocessRunner._sanitize_prompt(message)
        cmd = [
            "codex", "exec",
            "--json",
            "--ephemeral",
            "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
            "-C", str(self.workdir),
        ]
        if self.model:
            cmd += ["-m", self.model]
        cmd.append(message)

        log.debug("[%s] spawn ISOLATED codex: %s", self.name, " ".join(cmd[:6]))

        started = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(self.workdir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=self.timeout_s
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return RunResult(text="", error=f"codex timeout {self.timeout_s}s")
        except FileNotFoundError:
            return RunResult(text="", error="`codex` binary not found in PATH")
        except Exception as exc:
            log.exception("[%s] codex isolated subprocess crashed", self.name)
            return RunResult(text="", error=f"spawn failed: {exc}")

        duration_ms = int((time.monotonic() - started) * 1000)

        if proc.returncode != 0:
            err = (stderr or b"").decode("utf-8", errors="replace")[:2000]
            return RunResult(
                text="", error=f"codex exit {proc.returncode}: {err}", duration_ms=duration_ms
            )

        raw = (stdout or b"").decode("utf-8", errors="replace").strip()
        if not raw:
            return RunResult(text="", error="empty stdout", duration_ms=duration_ms)

        # Сохраняем session_id ДО парсинга — _parse_jsonl_output мутирует его.
        saved_sid = self._last_session_id
        result = self._parse_jsonl_output(raw, duration_ms)
        # Откатить session_id — isolated не должен мутировать state.
        result_clean = RunResult(
            text=result.text,
            usage=result.usage,
            session_id=None,
            cost_usd=result.cost_usd,
            duration_ms=result.duration_ms,
            error=result.error,
        )
        self._last_session_id = saved_sid
        return result_clean

    async def compact(self, *, session_id: Optional[str] = None) -> None:
        # Codex не имеет persistent session — compact пропускается bridge.
        # Метод оставлен для совместимости с AgentRunner интерфейсом.
        pass

    async def reset(self) -> None:
        self._last_session_id = None

    async def close(self) -> None:
        pass
