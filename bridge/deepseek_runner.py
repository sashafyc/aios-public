"""
deepseek_runner.py — раннер для DeepSeek API.

Интерфейс: AgentRunner (run, run_isolated, compact, reset, close).
DeepSeek API = OpenAI-compatible chat/completions через httpx.
Контекст: conversation history в памяти, compact обрезает, reset очищает.

Env: DEEPSEEK_API_KEY
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import AsyncGenerator, Optional

import httpx

from claude_runner import AgentRunner, RunResult, StreamEvent


log = logging.getLogger("bridge.deepseek_runner")

API_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_MODEL = "deepseek-v4-pro"
COST_PER_M = {"input": 0.435, "output": 0.87}
MAX_HISTORY_MESSAGES = 40
MAX_HISTORY_CHARS = 400_000


class DeepSeekRunner(AgentRunner):

    def __init__(
        self,
        name: str,
        workdir: Path,
        *,
        model: Optional[str] = None,
        timeout_s: int = 300,
    ):
        self.name = name
        self.workdir = Path(workdir)
        self.model = model or DEFAULT_MODEL
        self.timeout_s = timeout_s
        self._history: list[dict] = []
        self._system_prompt: Optional[str] = None
        self._last_session_id: Optional[str] = None
        # DeepSeek — API-runner: история живёт в памяти, поэтому при рестарте
        # моста контекст терялся (хотя в .state оставался session_id). Персистим
        # историю в JSONL рядом с агентом и подгружаем на старте.
        self._history_path = self.workdir / ".deepseek_history.jsonl"

        claude_md = self.workdir / "CLAUDE.md"
        if claude_md.exists():
            self._system_prompt = claude_md.read_text()[:8000]

        self._load_history()

    def _load_history(self) -> None:
        """Подгрузить историю диалога с диска (после рестарта моста)."""
        if not self._history_path.exists():
            return
        try:
            loaded: list[dict] = []
            for line in self._history_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    loaded.append(json.loads(line))
            self._history = loaded
            log.info("[%s] deepseek history restored: %d messages", self.name, len(loaded))
        except Exception:
            log.exception("[%s] failed to load deepseek history — starting fresh", self.name)
            self._history = []

    def _save_history(self) -> None:
        """Атомарно сохранить историю в JSONL (rewrite — история ограничена)."""
        try:
            self._history_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._history_path.with_suffix(".jsonl.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                for m in self._history:
                    f.write(json.dumps(m, ensure_ascii=False) + "\n")
            os.replace(tmp, self._history_path)
        except Exception:
            log.exception("[%s] failed to persist deepseek history", self.name)

    def _get_api_key(self) -> str:
        key = os.environ.get("DEEPSEEK_API_KEY", "")
        if not key:
            raise RuntimeError("DEEPSEEK_API_KEY not set")
        return key

    def _build_messages(self, user_message: str) -> list[dict]:
        msgs = []
        if self._system_prompt:
            msgs.append({"role": "system", "content": self._system_prompt})
        msgs.extend(self._history)
        msgs.append({"role": "user", "content": user_message})
        return msgs

    def _trim_history(self) -> None:
        if len(self._history) > MAX_HISTORY_MESSAGES * 2:
            self._history = self._history[-(MAX_HISTORY_MESSAGES * 2):]
        total = sum(len(m.get("content", "")) for m in self._history)
        while total > MAX_HISTORY_CHARS and len(self._history) > 2:
            total -= len(self._history.pop(0).get("content", ""))

    async def _call(self, messages: list[dict]) -> tuple[httpx.Response | None, str | None, int]:
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                resp = await client.post(
                    API_URL,
                    headers={
                        "Authorization": f"Bearer {self._get_api_key()}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "messages": messages,
                        "max_tokens": 8192,
                        "temperature": 0.7,
                        "stream": False,
                    },
                )
            return resp, None, int((time.monotonic() - started) * 1000)
        except httpx.TimeoutException:
            return None, f"deepseek timeout {self.timeout_s}s", int((time.monotonic() - started) * 1000)
        except Exception as exc:
            log.exception("[%s] deepseek request failed", self.name)
            return None, f"request failed: {exc}", int((time.monotonic() - started) * 1000)

    def _parse(self, resp: httpx.Response, duration_ms: int) -> RunResult:
        if resp.status_code != 200:
            err = resp.text[:500]
            log.error("[%s] deepseek HTTP %d: %s", self.name, resp.status_code, err)
            return RunResult(text="", error=f"HTTP {resp.status_code}: {err}", duration_ms=duration_ms)
        try:
            data = resp.json()
        except json.JSONDecodeError as exc:
            return RunResult(text="", error=f"json parse: {exc}", duration_ms=duration_ms)

        choices = data.get("choices", [])
        if not choices:
            return RunResult(text="", error="no choices", duration_ms=duration_ms)

        text = choices[0].get("message", {}).get("content", "")
        u = data.get("usage", {})
        usage = {"input": u.get("prompt_tokens", 0), "output": u.get("completion_tokens", 0),
                 "total": u.get("total_tokens", 0), "cached": u.get("prompt_cache_hit_tokens", 0)}
        cost = usage["input"] * COST_PER_M["input"] / 1e6 + usage["output"] * COST_PER_M["output"] / 1e6

        return RunResult(text=text[:16000], usage=usage, cost_usd=cost, duration_ms=duration_ms)

    async def run(self, message: str, *, resume: bool = True, session_id: Optional[str] = None) -> RunResult:
        # sanitize prompt
        if message and message.lstrip().startswith("-"):
            message = " " + message
        if session_id is None:
            self._history = []

        resp, error, ms = await self._call(self._build_messages(message))
        if error:
            return RunResult(text="", error=error, duration_ms=ms)

        result = self._parse(resp, ms)
        if not result.error:
            self._history.append({"role": "user", "content": message})
            self._history.append({"role": "assistant", "content": result.text})
            self._trim_history()
            self._save_history()
            if not self._last_session_id:
                self._last_session_id = f"ds-{self.name}-{int(time.time())}"
            result.session_id = self._last_session_id
        return result


    async def run_streaming(
        self,
        message: str,
        *,
        resume: bool = True,
        session_id: Optional[str] = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """SSE streaming: yield text deltas, then final result."""
        if session_id is None:
            self._history = []

        messages = self._build_messages(message)
        started = time.monotonic()
        accumulated = ""
        usage = {}

        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                async with client.stream(
                    "POST",
                    API_URL,
                    headers={
                        "Authorization": f"Bearer {self._get_api_key()}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "messages": messages,
                        "max_tokens": 8192,
                        "temperature": 0.7,
                        "stream": True,
                    },
                ) as resp:
                    if resp.status_code != 200:
                        body = await resp.aread()
                        err = body.decode(errors="replace")[:500]
                        yield StreamEvent(
                            kind="result",
                            result=RunResult(text="", error=f"HTTP {resp.status_code}: {err}",
                                             duration_ms=int((time.monotonic() - started) * 1000)),
                        )
                        return

                    async for line in resp.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        payload = line[6:]
                        if payload.strip() == "[DONE]":
                            break
                        try:
                            chunk = json.loads(payload)
                        except json.JSONDecodeError:
                            continue

                        delta = chunk.get("choices", [{}])[0].get("delta", {})
                        text_piece = delta.get("content", "")
                        if text_piece:
                            accumulated += text_piece
                            yield StreamEvent(kind="text_delta", text=text_piece)

                        if "usage" in chunk:
                            u = chunk["usage"]
                            usage = {
                                "input": u.get("prompt_tokens", 0),
                                "output": u.get("completion_tokens", 0),
                                "total": u.get("total_tokens", 0),
                                "cached": u.get("prompt_cache_hit_tokens", 0),
                            }

        except httpx.TimeoutException:
            ms = int((time.monotonic() - started) * 1000)
            yield StreamEvent(kind="result", result=RunResult(text=accumulated, error=f"timeout {self.timeout_s}s", duration_ms=ms))
            return
        except Exception as exc:
            log.exception("[%s] deepseek stream failed", self.name)
            ms = int((time.monotonic() - started) * 1000)
            yield StreamEvent(kind="result", result=RunResult(text=accumulated, error=f"stream error: {exc}", duration_ms=ms))
            return

        ms = int((time.monotonic() - started) * 1000)
        text_final = accumulated[:16000]
        cost = usage.get("input", 0) * COST_PER_M["input"] / 1e6 + usage.get("output", 0) * COST_PER_M["output"] / 1e6

        if not accumulated.strip():
            yield StreamEvent(kind="result", result=RunResult(text="", error="empty response", duration_ms=ms))
            return

        self._history.append({"role": "user", "content": message})
        self._history.append({"role": "assistant", "content": text_final})
        self._trim_history()
        self._save_history()
        if not self._last_session_id:
            self._last_session_id = f"ds-{self.name}-{int(time.time())}"

        yield StreamEvent(
            kind="result",
            result=RunResult(
                text=text_final,
                usage=usage,
                cost_usd=cost,
                duration_ms=ms,
                session_id=self._last_session_id,
            ),
        )

    async def run_isolated(self, message: str, *, session_id: Optional[str] = None) -> RunResult:
        msgs = []
        if self._system_prompt:
            msgs.append({"role": "system", "content": self._system_prompt})
        msgs.append({"role": "user", "content": message})
        resp, error, ms = await self._call(msgs)
        if error:
            return RunResult(text="", error=error, duration_ms=ms)
        return self._parse(resp, ms)

    async def compact(self, *, session_id: Optional[str] = None) -> None:
        if len(self._history) > 10:
            self._history = self._history[-10:]
            self._save_history()

    async def reset(self) -> None:
        self._history = []
        self._last_session_id = None
        try:
            self._history_path.unlink(missing_ok=True)
        except Exception:
            log.exception("[%s] failed to remove deepseek history on reset", self.name)

    async def close(self) -> None:
        self._history = []
