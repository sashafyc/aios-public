"""
delegation_router.py — парсер и маршрутизатор тегов в ответах агентов.

Форматы тегов (поддерживаем оба):
    однострочный:   [DELEGATE:research] Собери базу по X
    многострочный:  [DELEGATE:research]
                    Собери базу по X. Источники: ...
                    [/DELEGATE]

Поддерживаемые теги:
  [DELEGATE:<agent>[:task-id]]  — делегирование другому агенту
  [RESULT:<agent>:<task-id>]    — результат делегации обратно инициатору
  [WAITING_FOR:<agent>[:task-id], ...]  — агент уходит в WAITING
  [ASK_USER]                    — вопрос Саше
  [APPROVAL_NEEDED]             — запрос аппрува с inline-кнопками
  [STATUS]                      — произвольный статус-апдейт в свой топик
  [MEMORY_UPDATE]               — агент хочет обновить свой context.md
  [FILE:/path/to/file]           — агент хочет отправить файл пользователю в TG

Парсер ― регулярки с re.MULTILINE | re.DOTALL, устойчивые к незакрытым тегам.

Используют: tg_bridge после получения ответа от claude_runner.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Iterable, Optional


log = logging.getLogger("bridge.router")


# Одна регулярка на все теги. Захватываем:
#   group("tag")    — имя тега (DELEGATE, RESULT, ...)
#   group("args")   — аргументы после ":" до "]"  (может быть пусто)
#   group("body")   — тело многострочного блока (может быть пусто)
#
# Варианты:
#   [TAG] текст до конца строки                                  — single-line
#   [TAG:args] текст до конца строки                             — single-line с args
#   [TAG]...[/TAG]  /  [TAG:args]...[/TAG]                       — multi-line
_TAG_NAMES = r"DELEGATE|RESULT|WAITING_FOR|ASK_USER|APPROVAL_NEEDED|STATUS|MEMORY_UPDATE|FILE"

_TAG_RE = re.compile(
    r"\[(?P<tag>" + _TAG_NAMES + r")"
    r"(?::(?P<args>[^\]\n]*))?"
    r"\]"
    r"(?:"
        r"(?P<body>.*?)\[/(?P=tag)\]"   # многострочный блок
        r"|"
        r"(?P<inline>[^\n\[]*)"         # однострочный хвост до \n или следующего [
    r")",
    re.DOTALL,
)


@dataclass
class ParsedTag:
    """Один распарсенный тег из ответа агента."""

    tag: str                            # "DELEGATE" / "RESULT" / ...
    args: list[str] = field(default_factory=list)  # всё что было после ":" разбитое по ":"
    body: str = ""                      # содержимое тега (trimmed)
    span: tuple[int, int] = (0, 0)      # позиция в исходной строке — для вырезания

    @property
    def target_agent(self) -> Optional[str]:
        return self.args[0] if self.args else None

    @property
    def task_id(self) -> Optional[str]:
        return self.args[1] if len(self.args) > 1 else None


_CODE_BLOCK_RE = re.compile(r"```[\w+\-.]*\n?.*?```", re.DOTALL)


def _find_code_spans(text: str) -> list[tuple[int, int]]:
    """
    Вернуть список (start, end) для fenced code blocks.

    Inline backticks НЕ считаем защитной зоной: агенты часто случайно пишут
    `[RESULT:tasks:id]`, и такой тег должен работать как живая команда.
    Если нужен пример тега без выполнения — использовать fenced code block.
    """
    return [m.span() for m in _CODE_BLOCK_RE.finditer(text)]


def _is_inside_spans(pos: int, spans: list[tuple[int, int]]) -> bool:
    return any(s <= pos < e for s, e in spans)


def parse(text: str) -> list[ParsedTag]:
    """
    Вытащить все теги из текста. Возвращает список в порядке появления.

    Теги внутри fenced markdown code blocks ```...``` игнорируются —
    агент их туда положил как пример/цитату, а не как живую команду.
    Inline backticks вокруг тега допускаются и парсятся как команда.

    Неизвестные/битые теги игнорируются (они просто не матчатся регуляркой).
    """
    code_spans = _find_code_spans(text)
    out: list[ParsedTag] = []
    for m in _TAG_RE.finditer(text):
        if _is_inside_spans(m.start(), code_spans):
            # v9.2: подняли до INFO — bug #3 потенциально из-за того что Tasks
            # обернул [DELEGATE:...] в ```...``` блок. Видеть это в production.
            log.info(
                "tag %s at pos %d is inside fenced markdown code block — ignored as quote",
                m.group("tag"),
                m.start(),
            )
            continue
        tag = m.group("tag")
        args_raw = m.group("args") or ""
        args = [a.strip() for a in args_raw.split(":") if a.strip()]
        body = (m.group("body") if m.group("body") is not None else m.group("inline") or "").strip()
        out.append(ParsedTag(tag=tag, args=args, body=body, span=m.span()))
    return out


def strip_tags(text: str, tags: Iterable[ParsedTag]) -> str:
    """
    Вырезать теги из исходного текста чтобы получить 'чистый' текст для TG.
    Сохраняет порядок и переносы строк.

    Если тег был обёрнут в инлайн-бэктики (агент написал `[RESULT:tasks:id]`),
    strip_tags оставляет одинокий ` перед вырезанным местом; в финале
    подчищаем такие сиротские строки с одними бэктиками.
    """
    if not tags:
        return text.strip()
    # сортируем по позиции desc и вырезаем (от конца — чтобы не сломать offsets)
    spans = sorted((t.span for t in tags), key=lambda s: s[0], reverse=True)
    out = text
    for start, end in spans:
        out = out[:start] + out[end:]
    # сиротские backtick-строки после вырезания (например `<тег был здесь> → осталось `)
    out = re.sub(r"^[ \t]*`+[ \t]*$\n?", "", out, flags=re.MULTILINE)
    # схлопываем 3+ переносов строк в 2 и обрезаем пустые хвосты
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    # на всякий случай — финальный одиночный бэктик в конце
    out = re.sub(r"\n*`\s*$", "", out).strip()
    return out


# ──────────── helper'ы для bridge ────────────

def extract_waiting_for(tags: list[ParsedTag]) -> list[str]:
    """
    Из распарсенных тегов достать список ключей на которые агент ждёт ответ.
    Формат ключа — "<agent>:<task_id>" (task_id опционален).
    """
    keys: list[str] = []
    for t in tags:
        if t.tag != "WAITING_FOR":
            continue
        # [WAITING_FOR:research:task-x, scout:y] — может быть несколько через запятую
        raw = ":".join(t.args) if t.args else t.body
        for chunk in raw.split(","):
            chunk = chunk.strip()
            if chunk:
                keys.append(chunk)
    return keys


def extract_delegations(tags: list[ParsedTag]) -> list[ParsedTag]:
    return [t for t in tags if t.tag == "DELEGATE"]


def extract_results(tags: list[ParsedTag]) -> list[ParsedTag]:
    return [t for t in tags if t.tag == "RESULT"]


def has_ask_user(tags: list[ParsedTag]) -> bool:
    return any(t.tag == "ASK_USER" for t in tags)


def has_approval(tags: list[ParsedTag]) -> bool:
    return any(t.tag == "APPROVAL_NEEDED" for t in tags)



def extract_files(tags: list[ParsedTag]) -> list[ParsedTag]:
    return [t for t in tags if t.tag == "FILE"]

class DelegationRouter:
    """
    Тонкая обёртка поверх parse() — знает agents_registry и валидирует,
    что делегация разрешена (agent A → agent B допустимо по правилам).

    Используется tg_bridge после получения ответа от раннера:
        router.handle(from_agent, response_text) →
            dict с чистым текстом, списком исходящих сообщений, флагами.
    """

    def __init__(self, agents_registry_module):
        self._reg = agents_registry_module

    def handle(self, from_agent: str, response_text: str) -> dict:
        tags = parse(response_text)
        clean = strip_tags(response_text, tags)

        delegations: list[dict] = []
        for t in extract_delegations(tags):
            target = t.target_agent
            if not target or target not in self._reg.AGENTS:
                log.warning("delegation to unknown agent %r from %s — ignored", target, from_agent)
                continue
            if not self._is_allowed(from_agent, target):
                log.warning("delegation %s→%s not permitted by hierarchy — ignored", from_agent, target)
                continue
            delegations.append({
                "from": from_agent,
                "to": target,
                "task_id": t.task_id,
                "text": t.body,
            })

        results: list[dict] = []
        for t in extract_results(tags):
            target = t.target_agent
            if not target:
                continue
            # Fallback: если тело тега по сути пустое (агент написал
            # [RESULT:tasks:id] в конце без блочного содержимого, или обернул
            # в инлайн-бэктики так что inline body = "`") — отправляем
            # заказчику весь clean_text ответа. Это интуитивное поведение:
            # агент пишет полный результат, тегом лишь адресат+task_id.
            body_text = (t.body or "").strip()
            # если body содержит только backticks / whitespace — пусто
            if body_text.strip("`").strip() == "":
                body_text = ""
            if not body_text:
                body_text = clean
            results.append({
                "from": from_agent,
                "to": target,
                "task_id": t.task_id,
                "text": body_text,
            })

        return {
            "clean_text": clean,
            "delegations": delegations,
            "results": results,
            "waiting_for": extract_waiting_for(tags),
            "ask_user": has_ask_user(tags),
            "approval_needed": has_approval(tags),
            "files": extract_files(tags),
            "raw_tags": tags,
        }

    def _is_allowed(self, frm: str, to: str) -> bool:
        cfg = self._reg.AGENTS.get(frm)
        if not cfg:
            return False
        return to in cfg.can_delegate_to
