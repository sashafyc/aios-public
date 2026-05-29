"""
tg_formatter.py — конвертер "естественного" Markdown в Telegram HTML.

Зачем: агентам нельзя навязывать HTML-теги в CLAUDE.md (это жрёт их токены).
Они пишут обычный Markdown, а bridge конвертирует результат перед отправкой
в TG с parse_mode="HTML".

Используется в bridge._send_from() перед вызовом bot.send_message().

Подход:
  1. Вырезать блоки кода ```...``` и инлайн `...` в плейсхолдеры,
     чтобы их содержимое не трогалось как markdown.
  2. Вырезать markdown-таблицы в отдельные плейсхолдеры (сразу рендерим в <pre>).
  3. Экранировать HTML-спец символы (< > &) во всём остальном.
  4. Конвертировать markdown → HTML: заголовки, bold, italic, списки, ссылки, цитаты.
  5. Восстановить плейсхолдеры.

Поддерживаемые конструкции:
  **bold**, __bold__    → <b>...</b>
  *italic*, _italic_    → <i>...</i>
  ~~strike~~            → <s>...</s>
  ||spoiler||           → <tg-spoiler>...</tg-spoiler>
  `inline code`         → <code>...</code>
  ```code block```      → <pre>...</pre>   (с опциональным языком)
  # Заголовок (1-6)     → <b>Заголовок</b>
  - item / * item       → • item   (TG не умеет <ul>)
  1. item               → 1. item  (сохраняется как есть)
  [text](url)           → <a href="url">text</a>
  > цитата              → <blockquote>цитата</blockquote>
  | таблица |           → <pre>таблица с выровненными колонками</pre>
"""

from __future__ import annotations

import html
import re
from typing import Callable


# Невидимые плейсхолдер-символы чтобы отличать от любых реальных данных агента.
_PH_START = "\x00PH"
_PH_END = "\x00"


def format_for_telegram(text: str) -> str:
    """
    Преобразовать markdown из ответа агента в Telegram HTML subset.

    Никогда не бросает исключения: при неожиданном вводе возвращает лучший
    доступный результат. Вызывающий код при ошибке отправки должен fallback'нуться
    в plain text с parse_mode=None.
    """
    if not text:
        return ""

    placeholders: list[str] = []

    def save(html_snippet: str) -> str:
        idx = len(placeholders)
        placeholders.append(html_snippet)
        return f"{_PH_START}{idx}{_PH_END}"

    # ── 1. Блоки кода ```lang?\n...``` ──────────────────────────────────
    def _code_block(m: re.Match) -> str:
        lang = (m.group(1) or "").strip()
        content = m.group(2) or ""
        escaped = html.escape(content, quote=False)
        if lang and re.fullmatch(r"[A-Za-z0-9_+\-.]+", lang):
            return save(f'<pre><code class="language-{html.escape(lang, quote=True)}">{escaped}</code></pre>')
        return save(f"<pre>{escaped}</pre>")

    text = re.sub(
        r"```([A-Za-z0-9_+\-.]*)\n?(.*?)```",
        _code_block,
        text,
        flags=re.DOTALL,
    )

    # ── 2. Инлайн код `...` ─────────────────────────────────────────────
    def _inline_code(m: re.Match) -> str:
        content = m.group(1)
        return save(f"<code>{html.escape(content, quote=False)}</code>")

    text = re.sub(r"`([^`\n]+?)`", _inline_code, text)

    # ── 3. Markdown-таблицы → моноширинный <pre> блок ───────────────────
    def _table(m: re.Match) -> str:
        raw = m.group(0)
        rendered = _render_markdown_table(raw)
        if rendered is None:
            return raw
        return save(f"<pre>{html.escape(rendered, quote=False)}</pre>")

    table_re = re.compile(
        r"""
        (?:^|\n)                              # начало строки
        (\|[^\n]*\|[ \t]*\n                    # header:   | col | col |
         \|[ \t]*[:\-| \t]+[ \t]*\n             # sep:      |---|---|
         (?:\|[^\n]*\|[ \t]*(?:\n|$))+)         # rows:     | val | val |
        """,
        re.VERBOSE,
    )
    text = table_re.sub(lambda m: ("\n" if m.group(0).startswith("\n") else "") + _table(m), text)

    # ── 4. HTML escape всего оставшегося текста ─────────────────────────
    # На этом этапе в тексте только «обычный» markdown и плейсхолдеры.
    # Плейсхолдеры содержат \x00 — html.escape их не трогает.
    text = html.escape(text, quote=False)

    # ── 5. Заголовки # ## ### → <b>...</b> ──────────────────────────────
    text = re.sub(
        r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$",
        lambda m: f"<b>{m.group(2)}</b>",
        text,
        flags=re.MULTILINE,
    )

    # ── 6. Bold: **text** / __text__ ────────────────────────────────────
    text = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"<b>\1</b>", text, flags=re.DOTALL)
    text = re.sub(r"__(?=\S)(.+?)(?<=\S)__", r"<b>\1</b>", text, flags=re.DOTALL)

    # ── 7. Italic: *text* / _text_ ──────────────────────────────────────
    # Осторожно: не ломать уже собранные <b>...</b> и не трогать одиночные _
    # внутри слов (snake_case).
    text = re.sub(
        r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])",
        r"<i>\1</i>",
        text,
    )
    text = re.sub(
        r"(?<![\w_])_(?!\s)([^_\n]+?)(?<!\s)_(?![\w_])",
        r"<i>\1</i>",
        text,
    )

    # ── 8. Strikethrough ~~text~~ ───────────────────────────────────────
    text = re.sub(r"~~(?=\S)(.+?)(?<=\S)~~", r"<s>\1</s>", text, flags=re.DOTALL)

    # ── 9. Spoiler ||text|| ─────────────────────────────────────────────
    text = re.sub(r"\|\|(?=\S)(.+?)(?<=\S)\|\|", r"<tg-spoiler>\1</tg-spoiler>", text)

    # ── 10. Ссылки [text](url) ──────────────────────────────────────────
    # Важно: после html.escape в тексте будет &lt; &gt; и т.п., но скобки и
    # квадратные скобки не экранируются — поэтому markdown-синтаксис ссылок
    # остаётся рабочим.
    def _link(m: re.Match) -> str:
        label = m.group(1)
        url = m.group(2).strip()
        # href должен быть с экранированными кавычками если они там есть
        safe_url = url.replace('"', "&quot;")
        return f'<a href="{safe_url}">{label}</a>'

    text = re.sub(r"\[([^\]\n]+?)\]\(([^)\n]+?)\)", _link, text)

    # ── 11. Списки: ненумерованные → • ────────────────────────────────
    text = re.sub(
        r"^([ \t]*)[-*+][ \t]+(.+)$",
        lambda m: f"{m.group(1)}• {m.group(2)}",
        text,
        flags=re.MULTILINE,
    )
    # нумерованные оставляем как есть — они читаются в TG

    # ── 12. Цитаты > ... (многострочные блоки) ─────────────────────────
    # После html.escape символ '>' превратился в '&gt;', поэтому ищем его.
    def _quote(m: re.Match) -> str:
        block = m.group(0)
        lines = [re.sub(r"^[ \t]*&gt;[ \t]?", "", ln) for ln in block.split("\n")]
        inner = "\n".join(lines).strip("\n")
        return f"<blockquote>{inner}</blockquote>"

    text = re.sub(
        r"(?:^[ \t]*&gt;[ \t]?.*(?:\n[ \t]*&gt;[ \t]?.*)*)",
        _quote,
        text,
        flags=re.MULTILINE,
    )

    # ── 13. Восстановление плейсхолдеров ───────────────────────────────
    def _restore(m: re.Match) -> str:
        idx = int(m.group(1))
        if 0 <= idx < len(placeholders):
            return placeholders[idx]
        return m.group(0)

    text = re.sub(rf"{re.escape(_PH_START)}(\d+){re.escape(_PH_END)}", _restore, text)

    return text


def split_for_telegram(text: str, limit: int = 3800) -> list[str]:
    """
    Разбить длинный текст на куски по limit символов.

    ВАЖНО: вызывать ДО format_for_telegram, иначе можно порвать открывающий тег.

    Режет по (в порядке предпочтения):
      1. двойному переносу (граница параграфа)
      2. одиночному переносу
      3. пробелу
      4. жёстко по лимиту
    """
    if not text:
        return [""]
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break

        cut = remaining.rfind("\n\n", 0, limit)
        if cut == -1:
            cut = remaining.rfind("\n", 0, limit)
        if cut == -1:
            cut = remaining.rfind(" ", 0, limit)
        if cut == -1 or cut < limit // 2:
            cut = limit

        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip("\n ")

    return [c for c in chunks if c]


# ─────────────────────────── helpers ───────────────────────────

_MD_INSIDE_CELL_RE = re.compile(
    r"\*\*(.+?)\*\*"        # **bold**
    r"|__(.+?)__"            # __bold__
    r"|(?<!\w)\*(.+?)\*(?!\w)"  # *italic*
    r"|(?<!\w)_(.+?)_(?!\w)"    # _italic_
    r"|~~(.+?)~~"            # ~~strike~~
    r"|`(.+?)`"              # `code`
)


def _strip_cell_markdown(cell: str) -> str:
    """
    Убрать markdown-форматирование из текста ячейки таблицы.

    Таблицы рендерятся внутри <pre>-блока, где markdown НЕ интерпретируется —
    если агент написал **bold** в ячейке, звёздочки останутся видимы.
    Чище показать просто текст без маркеров.
    """
    def replace(m: re.Match) -> str:
        # возвращаем первую непустую группу (содержимое без маркеров)
        for g in m.groups():
            if g is not None:
                return g
        return m.group(0)
    return _MD_INSIDE_CELL_RE.sub(replace, cell)


def _render_markdown_table(raw: str) -> str | None:
    """
    Распарсить markdown-таблицу и отрисовать моноширинной с выровненными
    колонками. Возвращает готовую строку (без HTML-тегов) для помещения в <pre>,
    либо None если таблица не распарсилась.
    """
    lines = [ln for ln in raw.strip().split("\n") if ln.strip()]
    if len(lines) < 2:
        return None

    def split_row(line: str) -> list[str]:
        stripped = line.strip()
        if stripped.startswith("|"):
            stripped = stripped[1:]
        if stripped.endswith("|"):
            stripped = stripped[:-1]
        return [_strip_cell_markdown(c.strip()) for c in stripped.split("|")]

    header = split_row(lines[0])
    # separator — строка вида | --- | :---: | --- |
    sep_candidate = split_row(lines[1])
    if not all(re.fullmatch(r":?-{1,}:?", c) for c in sep_candidate if c):
        # нет разделителя — это не markdown таблица
        return None

    body = [split_row(ln) for ln in lines[2:]]
    all_rows = [header] + body

    # выравнивание по колонкам
    ncols = max(len(r) for r in all_rows)
    for r in all_rows:
        while len(r) < ncols:
            r.append("")

    col_widths = [max(len(row[c]) for row in all_rows) for c in range(ncols)]

    def render_row(row: list[str]) -> str:
        return "  ".join(cell.ljust(col_widths[i]) for i, cell in enumerate(row)).rstrip()

    lines_out = [render_row(header)]
    lines_out.append("  ".join("-" * w for w in col_widths).rstrip())
    for row in body:
        lines_out.append(render_row(row))

    return "\n".join(lines_out)
