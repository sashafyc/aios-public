"""
gemini_runner.py — раннер для Google Gemini CLI.

Интерфейс тот же, что у claude_runner.AgentRunner / codex_runner.CodexSubprocessRunner.

Gemini CLI: `gemini -p <msg> -y --output-format json [--resume latest] [--model <m>]`

Особенности:
  - History per-workdir хранится в `~/.gemini/projects/<workdir-encoded>/`,
    поэтому `--resume latest` подхватывает последнюю сессию ИМЕННО из этого workdir.
  - Параметр `session_id` от bridge ИСПОЛЬЗУЕТСЯ КАК ФЛАГ (не как UUID — gemini
    CLI не принимает UUID в --resume, только "latest" или индекс):
      session_id is None → bridge сообщает «холодный старт» (после /new или daily reset)
                          → запускаемся БЕЗ --resume
      session_id != None → есть persistent контекст → запускаемся с --resume latest
    Это синхронизирует gemini-агента с SessionManager: один источник правды о /new.
  - На бесплатном free tier (oauth-personal): 1000 запросов/день, 2M контекст.
  - Compact — no-op (нет persistent server-side session как у Claude Max).

Авторизация: `~/.gemini/settings.json` с selectedType=oauth-personal + сохранённый
OAuth-токен в `~/.gemini/oauth_creds.json`. См. memory/projects/gemini_cli.md.

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

from claude_runner import AgentRunner, RunResult


log = logging.getLogger("bridge.gemini_runner")


class GeminiSubprocessRunner(AgentRunner):
    """
    Тонкая обёртка над `gemini` CLI по аналогии с CodexSubprocessRunner.
    """

    def __init__(
        self,
        name: str,
        workdir: Path,
        *,
        model: Optional[str] = None,
        timeout_s: int = 600,
        yolo: bool = True,
    ):
        self.name = name
        self.workdir = Path(workdir)
        self.model = model  # None = пусть CLI сам выберет (Pro/Flash)
        self.timeout_s = timeout_s
        self.yolo = yolo
        self._last_session_id: Optional[str] = None

    def _build_cmd(self, message: str, *, resume: bool) -> list[str]:
        cmd = [
            "gemini",
            "-p", message,
            "--output-format", "json",
            "--skip-trust",  # под claude-agent в agent workdir по умолчанию untrusted
        ]
        if self.yolo:
            cmd += ["-y"]
        if resume:
            cmd += ["--resume", "latest"]
        if self.model:
            cmd += ["--model", self.model]
        return cmd

    async def run(
        self,
        message: str,
        *,
        resume: bool = True,
        session_id: Optional[str] = None,
    ) -> RunResult:
        # v9.8: sanitize prompt
        if message and message.lstrip().startswith("-"):
            message = " " + message
        # session_id is None → cold start (после /new или daily reset SessionManager).
        # session_id != None → есть контекст у SessionManager → подцепляемся через --resume latest.
        # gemini CLI не принимает UUID в --resume, только "latest" / индекс — поэтому
        # session_id используется как boolean-флаг наличия persistent контекста.
        do_resume = resume and session_id is not None
        cmd = self._build_cmd(message, resume=do_resume)
        log.debug("[%s] gemini spawn: resume=%s sid_passed=%s model=%s",
                  self.name, do_resume, bool(session_id), self.model or "auto")

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
                return RunResult(text="", error=f"gemini timeout {self.timeout_s}s")
        except FileNotFoundError:
            return RunResult(text="", error="`gemini` binary not found in PATH")
        except Exception as exc:
            log.exception("[%s] gemini subprocess crashed", self.name)
            return RunResult(text="", error=f"spawn failed: {exc}")

        duration_ms = int((time.monotonic() - started) * 1000)

        if proc.returncode != 0:
            err = (stderr or b"").decode("utf-8", errors="replace")[:2000]
            # Если resume сломался (нет истории на диске / повреждена) — fallback без resume.
            if do_resume and ("session" in err.lower() or "resume" in err.lower() or "not found" in err.lower()):
                log.warning("[%s] resume failed, retrying without resume: %s", self.name, err[:200])
                # Пере-вызываем себя с session_id=None → пойдёт без --resume.
                return await self.run(message, resume=resume, session_id=None)
            return RunResult(
                text="", error=f"gemini exit {proc.returncode}: {err}", duration_ms=duration_ms
            )

        raw = (stdout or b"").decode("utf-8", errors="replace").strip()
        if not raw:
            return RunResult(text="", error="empty stdout", duration_ms=duration_ms)

        return self._parse_json_output(raw, duration_ms, mutate_state=True)

    def _parse_json_output(self, raw: str, duration_ms: int, *, mutate_state: bool) -> RunResult:
        """
        Parse JSON output from `gemini -p ... --output-format json`.
        Вывод gemini может содержать предварительные строки (`YOLO mode is enabled.`,
        `Ripgrep is not available.`) перед JSON-объектом — ищем первую `{`.
        """
        json_start = raw.find("{")
        if json_start == -1:
            return RunResult(text="", error=f"no JSON in stdout: {raw[:300]}", duration_ms=duration_ms)
        try:
            data = json.loads(raw[json_start:])
        except json.JSONDecodeError as exc:
            return RunResult(text="", error=f"json parse failed: {exc}; raw={raw[json_start:][:300]}",
                             duration_ms=duration_ms)

        text = data.get("response", "")
        sid = data.get("session_id")
        # usage из stats.models.<model>.tokens — берём первую модель в списке.
        usage = {}
        models = (data.get("stats") or {}).get("models") or {}
        if models:
            first_model_stats = next(iter(models.values()))
            tokens = first_model_stats.get("tokens") or {}
            usage = {
                "input": tokens.get("input", 0),
                "output": tokens.get("candidates", 0),
                "cached": tokens.get("cached", 0),
                "total": tokens.get("total", 0),
                "thoughts": tokens.get("thoughts", 0),
            }

        if mutate_state and sid:
            self._last_session_id = sid

        return RunResult(
            text=text[:16000] if isinstance(text, str) else "",
            usage=usage,
            session_id=sid,
            cost_usd=0.0,  # free tier
            duration_ms=duration_ms,
        )

    async def run_isolated(self, message: str, *, session_id: Optional[str] = None) -> RunResult:
        """
        Одноразовый прогон БЕЗ --resume и без мутации внутреннего состояния.
        Используется для cron-триггеров.
        """
        cmd = [
            "gemini",
            "-p", message,
            "--output-format", "json",
            "--skip-trust",
        ]
        if self.yolo:
            cmd += ["-y"]
        if self.model:
            cmd += ["--model", self.model]

        log.debug("[%s] spawn ISOLATED gemini: %s", self.name, " ".join(cmd[:5]))

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
                return RunResult(text="", error=f"gemini timeout {self.timeout_s}s")
        except FileNotFoundError:
            return RunResult(text="", error="`gemini` binary not found in PATH")
        except Exception as exc:
            log.exception("[%s] gemini isolated subprocess crashed", self.name)
            return RunResult(text="", error=f"spawn failed: {exc}")

        duration_ms = int((time.monotonic() - started) * 1000)

        if proc.returncode != 0:
            err = (stderr or b"").decode("utf-8", errors="replace")[:2000]
            return RunResult(
                text="", error=f"gemini exit {proc.returncode}: {err}", duration_ms=duration_ms
            )

        raw = (stdout or b"").decode("utf-8", errors="replace").strip()
        if not raw:
            return RunResult(text="", error="empty stdout", duration_ms=duration_ms)

        return self._parse_json_output(raw, duration_ms, mutate_state=False)

    async def compact(self, *, session_id: Optional[str] = None) -> None:
        # Gemini не имеет persistent server-side session — compact пропускается bridge.
        # Метод оставлен для совместимости с AgentRunner интерфейсом.
        pass

    async def reset(self) -> None:
        # Поведение reset для gemini обеспечивается через session_id=None от bridge:
        # после `sessions.on_reset_to_idle()` SessionManager отдаёт None → run() идёт
        # без --resume → cold start. Эта функция нужна только для совместимости
        # с AgentRunner интерфейсом и для ручного вызова из cron-триггеров.
        self._last_session_id = None

    async def close(self) -> None:
        pass
