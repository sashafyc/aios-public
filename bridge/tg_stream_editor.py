"""
tg_stream_editor.py — потоковая отправка текста в TG с debounce.

Управляет одним (или несколькими при split) TG-сообщением:
  - Первый чанк → send_message
  - Далее → edit_message_text с debounce 800ms
  - При >3800 chars → финализация текущего, send_message нового
  - finalize() → гарантированный финальный edit

Используется в tg_bridge при stream=True в AgentConfig.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Optional

from tg_formatter import format_for_telegram, split_for_telegram

log = logging.getLogger("bridge.stream_editor")

# Минимальный интервал между edit_message_text (мс)
DEBOUNCE_MS = 2500
# Лимит символов для одного TG-сообщения. Запас от 4096:
# - HTML-теги после format_for_telegram могут раздуть длину (**x** → <b>x</b>).
# - Поэтому 3500, а не 3800 как было до .
CHUNK_LIMIT = 3500


class TgStreamEditor:
    """
    Потоковый редактор TG-сообщения.

    Использование:
        editor = TgStreamEditor(bot, chat_id, topic_id)
        await editor.push("первая часть")
        await editor.push("ещё текст")
        await editor.push_tool("Read", "src/app.py")
        await editor.finalize()
        # или
        await editor.finalize_with_clean(clean_text)
    """

    def __init__(self, bot, chat_id: int, topic_id: int):
        self._bot = bot
        self._chat_id = chat_id
        self._topic_id = topic_id

        self._accumulated = ""
        self._msg_id: Optional[int] = None
        self._msg_start_offset = 0
        self._last_edit_ts = 0.0
        self._edit_task: Optional[asyncio.Task] = None
        self._sent_text = ""
        self._tool_lines: list[tuple[str, int]] = []  # (tool_name, count)
        self._flood_until: float = 0.0

    @property
    def accumulated(self) -> str:
        return self._accumulated

    async def push(self, text_delta: str) -> None:
        """Добавить текстовую дельту."""
        if not text_delta:
            return
        self._accumulated += text_delta

        current_chunk = self._accumulated[self._msg_start_offset:]

        # Чанк слишком длинный — финализируем и начинаем новое сообщение
        if len(current_chunk) > CHUNK_LIMIT and self._msg_id is not None:
            await self._finalize_current_msg()
            self._msg_start_offset = len(self._accumulated) - len(text_delta)
            self._msg_id = None
            self._sent_text = ""

        if self._msg_id is None:
            await self._send_new()
        else:
            await self._schedule_edit()

    async def push_tool(self, tool_name: str, tool_input: str = "") -> None:
        """Показать плашку выполняемого инструмента (группирует последовательные вызовы)."""
        if self._tool_lines and self._tool_lines[-1][0] == tool_name:
            name, count = self._tool_lines[-1]
            self._tool_lines[-1] = (name, count + 1)
        else:
            self._tool_lines.append((tool_name, 1))
        if self._msg_id is not None:
            await self._do_edit(force=True)

    async def clear_tools(self) -> None:
        """Убрать плашки тулов (когда тул завершился и пошёл текст)."""
        if self._tool_lines:
            self._tool_lines.clear()

    async def finalize(self) -> str:
        """
        Финальный edit с полным текстом. Возвращает accumulated.
        """
        await self._cancel_pending_edit()
        self._tool_lines.clear()

        if self._msg_id is not None:
            await self._do_edit(force=True, final=True)

        return self._accumulated

    async def finalize_with_clean(self, clean_text: str) -> None:
        """
        Финальный edit с очищенным текстом (без тегов делегаций).
        Вызывается после router.handle().

        Split на N сообщений если clean_text > CHUNK_LIMIT (предыдущая
        реализация слайсила по offset'у raw-текста — некорректно когда теги
        вырезаются разной длины — и не дробила вообще, из-за чего финальный
        edit падал MESSAGE_TOO_LONG, ничего в TG не показывалось.
        """
        await self._cancel_pending_edit()
        self._tool_lines.clear()

        if not clean_text or not clean_text.strip():
            # Нечего отправлять (например, ответ был только теги). Оставляем
            # текущее сообщение как есть (там накоплен raw без тегов).
            if self._msg_id is not None:
                await self._do_edit(force=True, final=True)
            return

        # Делим clean_text на TG-чанки (с запасом на HTML-раздув)
        chunks = split_for_telegram(clean_text, limit=CHUNK_LIMIT)
        chunks = [c for c in chunks if c.strip()]
        if not chunks:
            return

        # Если ещё не было сообщения (необычно — обычно push уже сделал send_new)
        # — просто шлём все чанки новыми send_message.
        if self._msg_id is None:
            for chunk in chunks:
                msg = await self._try_send(chunk)
                if msg:
                    self._msg_id = msg.message_id
                    self._sent_text = chunk
                    self._last_edit_ts = time.monotonic()
            return

        # Первый чанк → edit текущего сообщения. Остальные → новые send_message.
        await self._edit_with_text(chunks[0], final=True)
        for chunk in chunks[1:]:
            msg = await self._try_send(chunk)
            if msg:
                # Переключаемся на новое сообщение для возможных будущих edit'ов
                self._msg_id = msg.message_id
                self._sent_text = chunk
                self._last_edit_ts = time.monotonic()

    # ─── internal ─────────────────────────────────────────────

    async def _send_new(self) -> None:
        """Отправить первое/новое сообщение."""
        current = self._current_display_text()
        if not current.strip():
            current = "▍"  # cursor placeholder

        msg = await self._try_send(current)
        if msg:
            self._msg_id = msg.message_id
            self._sent_text = current
            self._last_edit_ts = time.monotonic()

    async def _schedule_edit(self) -> None:
        """Запланировать edit с debounce."""
        now = time.monotonic()
        elapsed_ms = (now - self._last_edit_ts) * 1000

        if elapsed_ms >= DEBOUNCE_MS:
            await self._do_edit()
        else:
            if self._edit_task and not self._edit_task.done():
                return
            delay = (DEBOUNCE_MS - elapsed_ms) / 1000
            self._edit_task = asyncio.create_task(self._delayed_edit(delay))

    async def _delayed_edit(self, delay: float) -> None:
        await asyncio.sleep(delay)
        await self._do_edit()

    async def _do_edit(self, *, force: bool = False, final: bool = False) -> None:
        if self._msg_id is None:
            return

        current = self._current_display_text()
        if not force and current == self._sent_text:
            return

        await self._edit_with_text(current, final=final)

    async def _edit_with_text(self, text: str, *, final: bool = False) -> None:
        """Отредактировать текущее сообщение с заданным текстом."""
        if self._msg_id is None:
            return

        # Flood control backoff: промежуточные апдейты пропускаем, финальный ждём
        now = time.monotonic()
        if now < self._flood_until:
            if not final:
                return
            wait = self._flood_until - now + 0.5
            log.info("stream_editor: flood backoff %.1fs (final edit)", wait)
            await asyncio.sleep(wait)

        try:
            formatted = format_for_telegram(text)
            await self._bot.edit_message_text(
                text=formatted,
                chat_id=self._chat_id,
                message_id=self._msg_id,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            self._sent_text = text
            self._last_edit_ts = time.monotonic()
        except Exception as exc:
            exc_str = str(exc)
            if "not modified" in exc_str.lower():
                return
            # safety net — если всё же поймали MESSAGE_TOO_LONG,
            # обрезаем до CHUNK_LIMIT и шлём plain. Лучше потерять хвост,
            # чем оставить пустое сообщение.
            if "message_too_long" in exc_str.lower() or "MESSAGE_TOO_LONG" in exc_str:
                truncated = text[:CHUNK_LIMIT - 30] + "\n…(обрезано)"
                try:
                    await self._bot.edit_message_text(
                        text=truncated,
                        chat_id=self._chat_id,
                        message_id=self._msg_id,
                        parse_mode=None,
                        disable_web_page_preview=True,
                    )
                    self._sent_text = truncated
                    self._last_edit_ts = time.monotonic()
                    log.warning("stream_editor: truncated %d→%d chars (MESSAGE_TOO_LONG safety net)",
                                len(text), len(truncated))
                except Exception:
                    log.exception("stream_editor: truncated edit also failed")
                return
            m = re.search(r"retry in (\d+) seconds", exc_str, re.IGNORECASE)
            if m:
                retry_after = int(m.group(1)) + 2
                self._flood_until = time.monotonic() + retry_after
                log.warning("stream_editor: flood control, backoff %ds (final=%s)", retry_after, final)
                if final:
                    await asyncio.sleep(retry_after)
                    try:
                        await self._bot.edit_message_text(
                            text=text,
                            chat_id=self._chat_id,
                            message_id=self._msg_id,
                            parse_mode=None,
                            disable_web_page_preview=True,
                        )
                        self._sent_text = text
                        self._last_edit_ts = time.monotonic()
                    except Exception:
                        log.exception("stream_editor: final edit failed after flood wait")
                return
            log.warning("stream edit HTML failed (%s), trying plain", exc)
            try:
                await self._bot.edit_message_text(
                    text=text,
                    chat_id=self._chat_id,
                    message_id=self._msg_id,
                    parse_mode=None,
                    disable_web_page_preview=True,
                )
                self._sent_text = text
                self._last_edit_ts = time.monotonic()
            except Exception:
                log.exception("stream edit plain also failed")

    async def _try_send(self, text: str, _retries: int = 5):
        """Отправить новое сообщение. Retry при FloodControl с exponential backoff."""
        for attempt in range(_retries):
            try:
                fmt = format_for_telegram(text) if attempt == 0 else text
                pm = "HTML" if attempt == 0 else None
                return await self._bot.send_message(
                    chat_id=self._chat_id,
                    message_thread_id=self._topic_id,
                    text=fmt,
                    parse_mode=pm,
                    disable_web_page_preview=True,
                )
            except Exception as exc:
                exc_str = str(exc)
                m = re.search(r"retry in (\d+) seconds", exc_str, re.IGNORECASE)
                if m:
                    wait = int(m.group(1)) + 2
                    self._flood_until = time.monotonic() + wait
                    log.warning("stream_editor: send flood control, backoff %ds (attempt %d/%d)",
                                wait, attempt + 1, _retries)
                    await asyncio.sleep(wait)
                    continue
                if attempt == 0:
                    continue
                log.exception("stream send failed after %d attempts", attempt + 1)
                return None
        log.error("stream send LOST after %d flood retries: %s", _retries, text[:100])
        return None

    async def _finalize_current_msg(self) -> None:
        """Финализировать текущее сообщение перед split."""
        await self._cancel_pending_edit()
        self._tool_lines.clear()
        await self._do_edit(force=True)

    async def _cancel_pending_edit(self) -> None:
        if self._edit_task and not self._edit_task.done():
            self._edit_task.cancel()
            try:
                await self._edit_task
            except (asyncio.CancelledError, Exception):
                pass

    def _current_display_text(self) -> str:
        text = self._accumulated[self._msg_start_offset:]
        if self._tool_lines:
            parts = []
            for name, count in self._tool_lines:
                parts.append(f"⚙️ {name} ×{count}" if count > 1 else f"⚙️ {name}")
            tool_block = "\n".join(parts)
            text = text.rstrip() + "\n\n" + tool_block
        return text
