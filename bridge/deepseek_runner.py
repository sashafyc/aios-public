"""
deepseek_runner.py — агентский раннер DeepSeek через CodeWhale CLI.

Единый раннер «всё в одном» (как claude/codex): реальные инструменты
(--auto: чтение/запись файлов, shell), память диалога через сессии
(--resume <session_id>), стриминг (--output-format stream-json).
Заменяет прежний chat-API раннер (без инструментов).

CLI: `codewhale` (преемник deprecated deepseek-tui; npm i -g codewhale).
Бинарь: /usr/bin/codewhale. Модель по умолчанию: deepseek-v4-pro.
Env: DEEPSEEK_API_KEY (наследуется subprocess'ом из окружения bridge).

stream-json события CodeWhale:
  {"type":"content","content":"..."}                  — текстовая дельта
  {"type":"session_capture","content":"<uuid>"}        — id сессии
  {"type":"metadata","meta":{"session_id","input_tokens","output_tokens","status"}}
  {"type":"tool", ...}                                 — вызов инструмента (если есть)
  {"type":"error","error":"..."} / {"type":"done"}
Вывод может содержать ANSI/terminal-escape мусор — парсим от первого '{'.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import AsyncGenerator, Optional

from claude_runner import AgentRunner, RunResult, StreamEvent
import pricing as _pricing


log = logging.getLogger("bridge.deepseek_runner")

CODEWHALE_BIN = os.environ.get("CODEWHALE_BIN", "/usr/bin/codewhale")
DEFAULT_MODEL = "deepseek-v4-pro"


class DeepSeekRunner(AgentRunner):
    """Агентский DeepSeek через CodeWhale CLI: руки + сессии + стриминг."""

    def __init__(
        self,
        name: str,
        workdir: Path,
        *,
        model: Optional[str] = None,
        timeout_s: int = 2400,
    ):
        self.name = name
        self.workdir = Path(workdir)
        self.model = model or DEFAULT_MODEL
        self.timeout_s = timeout_s
        self._last_session_id: Optional[str] = None
        self._system_prompt: Optional[str] = None

        claude_md = self.workdir / "CLAUDE.md"
        if claude_md.exists():
            try:
                self._system_prompt = claude_md.read_text(encoding="utf-8")[:8000]
            except Exception:
                self._system_prompt = None

        # session id переживает рестарт моста
        self._sid_path = self.workdir / ".codewhale_session"
        if self._sid_path.exists():
            try:
                sid = self._sid_path.read_text(encoding="utf-8").strip()
                self._last_session_id = sid or None
            except Exception:
                self._last_session_id = None

    # ------------------------------------------------------------------ utils
    @staticmethod
    def _sanitize(message: str) -> str:
        if message and message.lstrip().startswith("-"):
            return " " + message
        return message

    def _save_sid(self) -> None:
        try:
            if self._last_session_id:
                self._sid_path.parent.mkdir(parents=True, exist_ok=True)
                self._sid_path.write_text(self._last_session_id, encoding="utf-8")
        except Exception:
            log.exception("[%s] failed to persist codewhale session id", self.name)

    def _build_cmd(self, prompt: str, sid: Optional[str]) -> list[str]:
        cmd = [
            CODEWHALE_BIN, "exec", "--auto",
            "--output-format", "stream-json",
            "--model", self.model,
        ]
        if sid:
            cmd += ["--resume", sid]
        cmd.append(prompt)
        return cmd

    def _prepare_prompt(self, message: str, sid: Optional[str]) -> str:
        msg = self._sanitize(message)
        # system-роль подмешиваем только в НОВУЮ сессию; при resume она уже в истории.
        if self._system_prompt and not sid:
            return f"{self._system_prompt}\n\n---\n# ТЕКУЩАЯ ЗАДАЧА\n{msg}"
        return msg

    @staticmethod
    def _parse_event(line: str) -> Optional[dict]:
        i = line.find("{")
        if i < 0:
            return None
        try:
            return json.loads(line[i:].strip())
        except json.JSONDecodeError:
            return None

    # --------------------------------------------------------------- core run
    async def _spawn(self, prompt: str, sid: Optional[str]):
        cmd = self._build_cmd(prompt, sid)
        log.debug("[%s] codewhale spawn (resume=%s, model=%s)", self.name, bool(sid), self.model)
        return await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(self.workdir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    def _finalize(self, parts: list[str], meta: dict, sid_capture: Optional[str],
                  err: Optional[str], duration_ms: int, *, isolated: bool) -> RunResult:
        text = "".join(parts)
        session_id = (meta.get("session_id") or sid_capture or None)
        status = meta.get("status")
        usage = {}
        cost = 0.0
        if meta:
            it = int(meta.get("input_tokens", 0) or 0)
            ot = int(meta.get("output_tokens", 0) or 0)
            usage = {"input_tokens": it, "output_tokens": ot, "total": it + ot}
            cost = _pricing.cost_usd(self.model, it, ot)
        if err or (status not in ("completed", None, "")):
            return RunResult(text=text[:16000], error=(err or f"status={status}"),
                             usage=usage, cost_usd=cost, duration_ms=duration_ms)
        # запоминаем сессию (кроме isolated — он не должен мутировать state)
        if session_id and not isolated:
            self._last_session_id = session_id
            self._save_sid()
        return RunResult(
            text=text[:16000], usage=usage, cost_usd=cost,
            duration_ms=duration_ms,
            session_id=(None if isolated else session_id),
        )

    async def _run_once(self, message: str, *, sid: Optional[str], isolated: bool) -> RunResult:
        prompt = self._prepare_prompt(message, sid)
        started = time.monotonic()
        try:
            proc = await self._spawn(prompt, sid)
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.timeout_s)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return RunResult(text="", error=f"codewhale timeout {self.timeout_s}s")
        except FileNotFoundError:
            return RunResult(text="", error=f"`{CODEWHALE_BIN}` not found in PATH")
        except Exception as exc:
            log.exception("[%s] codewhale subprocess crashed", self.name)
            return RunResult(text="", error=f"spawn failed: {exc}")

        duration_ms = int((time.monotonic() - started) * 1000)
        if proc.returncode != 0:
            errtxt = (stderr or b"").decode("utf-8", errors="replace")[:2000]
            # сессия протухла → один откат на новую
            if sid and ("not found" in errtxt.lower() or "invalid" in errtxt.lower()
                        or "no session" in errtxt.lower()):
                log.warning("[%s] resume failed (stale sid), retry new session", self.name)
                self._last_session_id = None
                return await self._run_once(message, sid=None, isolated=isolated)
            return RunResult(text="", error=f"codewhale exit {proc.returncode}: {errtxt}",
                             duration_ms=duration_ms)

        raw = (stdout or b"").decode("utf-8", errors="replace")
        parts: list[str] = []
        meta: dict = {}
        sid_capture: Optional[str] = None
        err: Optional[str] = None
        tools_used = 0
        for line in raw.splitlines():
            ev = self._parse_event(line)
            if not ev:
                continue
            t = ev.get("type")
            if t == "content":
                parts.append(ev.get("content", ""))
            elif t == "session_capture":
                sid_capture = ev.get("content") or sid_capture
            elif t == "metadata":
                meta = ev.get("meta", {}) or {}
            elif t == "tool":
                tools_used += 1
            elif t == "error":
                err = ev.get("error") or "codewhale error"
        if tools_used:
            log.info("[%s] codewhale tools=%d", self.name, tools_used)
        return self._finalize(parts, meta, sid_capture, err, duration_ms, isolated=isolated)

    # -------------------------------------------------------------- interface
    async def run(self, message: str, *, resume: bool = True, session_id: Optional[str] = None) -> RunResult:
        sid = session_id or (self._last_session_id if resume else None)
        return await self._run_once(message, sid=sid, isolated=False)

    async def run_isolated(self, message: str, *, session_id: Optional[str] = None) -> RunResult:
        # cron/one-shot — без resume и без мутации session-state
        return await self._run_once(message, sid=None, isolated=True)

    async def run_streaming(
        self,
        message: str,
        *,
        resume: bool = True,
        session_id: Optional[str] = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Стрим текстовых дельт из stream-json, затем финальный RunResult."""
        sid = session_id or (self._last_session_id if resume else None)
        prompt = self._prepare_prompt(message, sid)
        started = time.monotonic()
        parts: list[str] = []
        meta: dict = {}
        sid_capture: Optional[str] = None
        err: Optional[str] = None

        try:
            proc = await self._spawn(prompt, sid)
        except FileNotFoundError:
            yield StreamEvent(kind="result", result=RunResult(text="", error=f"`{CODEWHALE_BIN}` not found"))
            return
        except Exception as exc:
            yield StreamEvent(kind="result", result=RunResult(text="", error=f"spawn failed: {exc}"))
            return

        try:
            while True:
                try:
                    line = await asyncio.wait_for(proc.stdout.readline(), timeout=self.timeout_s)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
                    yield StreamEvent(kind="result", result=RunResult(
                        text="".join(parts), error=f"codewhale timeout {self.timeout_s}s",
                        duration_ms=int((time.monotonic() - started) * 1000)))
                    return
                if not line:
                    break
                ev = self._parse_event(line.decode("utf-8", errors="replace"))
                if not ev:
                    continue
                t = ev.get("type")
                if t == "content":
                    piece = ev.get("content", "")
                    if piece:
                        parts.append(piece)
                        yield StreamEvent(kind="text_delta", text=piece)
                elif t == "session_capture":
                    sid_capture = ev.get("content") or sid_capture
                elif t == "metadata":
                    meta = ev.get("meta", {}) or {}
                elif t == "error":
                    err = ev.get("error") or "codewhale error"
            await proc.wait()
        except Exception as exc:
            log.exception("[%s] codewhale stream crashed", self.name)
            yield StreamEvent(kind="result", result=RunResult(
                text="".join(parts), error=f"stream error: {exc}",
                duration_ms=int((time.monotonic() - started) * 1000)))
            return

        duration_ms = int((time.monotonic() - started) * 1000)
        result = self._finalize(parts, meta, sid_capture, err, duration_ms, isolated=False)
        yield StreamEvent(kind="result", result=result)

    async def compact(self, *, session_id: Optional[str] = None) -> None:
        # история живёт в сессии CodeWhale на стороне CLI — compact не требуется
        pass

    async def reset(self) -> None:
        self._last_session_id = None
        try:
            self._sid_path.unlink(missing_ok=True)
        except Exception:
            pass

    async def close(self) -> None:
        pass
