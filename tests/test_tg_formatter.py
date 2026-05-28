"""
Tests for bridge.tg_formatter: Markdown → Telegram HTML.

Покрывают базовые конструкции, edge cases, защиту от инъекций HTML в тексте.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))

import tg_formatter as fmt  # noqa: E402


# ───────────────── escape ─────────────────

def test_html_escape_plain_text():
    assert fmt.format_for_telegram("a < b & c > d") == "a &lt; b &amp; c &gt; d"


def test_empty_string():
    assert fmt.format_for_telegram("") == ""


def test_plain_text_unchanged():
    assert fmt.format_for_telegram("just plain text") == "just plain text"


# ───────────────── bold / italic / strike ─────────────────

def test_bold_stars():
    assert fmt.format_for_telegram("**bold**") == "<b>bold</b>"


def test_bold_underscores():
    assert fmt.format_for_telegram("__bold__") == "<b>bold</b>"


def test_italic_star():
    assert fmt.format_for_telegram("*italic*") == "<i>italic</i>"


def test_italic_underscore():
    assert fmt.format_for_telegram("an _italic_ word") == "an <i>italic</i> word"


def test_italic_does_not_break_snake_case():
    assert fmt.format_for_telegram("my_var_name") == "my_var_name"


def test_bold_and_italic_mixed():
    got = fmt.format_for_telegram("**bold** and *ital*")
    assert got == "<b>bold</b> and <i>ital</i>"


def test_strike():
    assert fmt.format_for_telegram("~~cut~~") == "<s>cut</s>"


def test_spoiler():
    assert fmt.format_for_telegram("||hidden||") == "<tg-spoiler>hidden</tg-spoiler>"


# ───────────────── inline / block code ─────────────────

def test_inline_code():
    assert fmt.format_for_telegram("run `python3 main.py`") == "run <code>python3 main.py</code>"


def test_inline_code_preserves_markdown_inside():
    # содержимое инлайна не должно быть интерпретировано как markdown
    got = fmt.format_for_telegram("use `**not bold**` here")
    assert "<code>**not bold**</code>" in got
    assert "<b>" not in got


def test_code_block_no_lang():
    got = fmt.format_for_telegram("```\nhello world\n```")
    assert got == "<pre>hello world\n</pre>"


def test_code_block_with_lang():
    got = fmt.format_for_telegram("```python\nprint(1)\n```")
    assert got == '<pre><code class="language-python">print(1)\n</code></pre>'


def test_code_block_escapes_html():
    got = fmt.format_for_telegram("```\n<script>alert(1)</script>\n```")
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in got


def test_code_block_does_not_process_inner_markdown():
    got = fmt.format_for_telegram("```\n**not bold** # not heading\n```")
    assert "<b>" not in got
    assert "**not bold**" in got


# ───────────────── headings ─────────────────

def test_heading_h1():
    assert fmt.format_for_telegram("# Hello") == "<b>Hello</b>"


def test_heading_h2():
    assert fmt.format_for_telegram("## Subtitle") == "<b>Subtitle</b>"


def test_heading_h6():
    assert fmt.format_for_telegram("###### small") == "<b>small</b>"


def test_heading_in_mixed_text():
    got = fmt.format_for_telegram("intro\n\n# Title\n\nbody")
    assert "<b>Title</b>" in got
    assert "intro" in got and "body" in got


# ───────────────── lists ─────────────────

def test_dash_list():
    got = fmt.format_for_telegram("- one\n- two\n- three")
    assert got == "• one\n• two\n• three"


def test_star_list():
    got = fmt.format_for_telegram("* one\n* two")
    assert got == "• one\n• two"


def test_numbered_list_unchanged():
    got = fmt.format_for_telegram("1. first\n2. second")
    assert got == "1. first\n2. second"


def test_indented_list():
    got = fmt.format_for_telegram("  - nested")
    assert got == "  • nested"


# ───────────────── links ─────────────────

def test_link():
    got = fmt.format_for_telegram("[click](https://a.inv)")
    assert got == '<a href="https://a.inv">click</a>'


def test_link_with_special_chars_in_label():
    got = fmt.format_for_telegram("[a & b](https://x)")
    assert '<a href="https://x">a &amp; b</a>' == got


# ───────────────── quotes ─────────────────

def test_single_quote():
    got = fmt.format_for_telegram("> someone said this")
    assert got == "<blockquote>someone said this</blockquote>"


def test_multiline_quote():
    got = fmt.format_for_telegram("> line one\n> line two")
    assert got == "<blockquote>line one\nline two</blockquote>"


# ───────────────── tables ─────────────────

def test_simple_table_becomes_pre_block():
    md = "| col a | col b |\n| --- | --- |\n| 1 | 2 |\n| 3 | 4 |"
    got = fmt.format_for_telegram(md)
    assert got.startswith("<pre>")
    assert got.endswith("</pre>")
    assert "col a" in got
    assert "col b" in got
    assert "1" in got and "4" in got
    # разделитель ----
    assert "---" in got


def test_table_strips_markdown_in_cells():
    """
    Markdown-форматирование внутри ячеек (например **bold**) должно быть
    убрано, т.к. таблица рендерится в <pre>-блоке где markdown не работает.
    """
    md = (
        "| Сервис | Выручка |\n"
        "|---|---|\n"
        "| **MPSTATS** | 1.96 млрд |\n"
        "| *MarketGuru* | 562 млн |\n"
    )
    got = fmt.format_for_telegram(md)
    assert "**" not in got
    assert "MPSTATS" in got
    assert "MarketGuru" in got
    # окружение — <pre>
    assert got.startswith("<pre>")


def test_table_columns_align():
    md = "| short | long column |\n| --- | --- |\n| a | b |"
    got = fmt.format_for_telegram(md)
    # ширина short — 5, long column — 11; строки должны содержать выравнивание
    assert "short" in got
    assert "long column" in got
    # 'a' должно иметь дополнительные пробелы (ljust до 5 символов)
    assert "a    " in got or "a  " in got


# ───────────────── mixed ─────────────────

def test_mixed_bold_and_list_and_code():
    text = "# Report\n\n- **done**: `tg_formatter.py`\n- todo: `bridge.py`"
    got = fmt.format_for_telegram(text)
    assert "<b>Report</b>" in got
    assert "• <b>done</b>: <code>tg_formatter.py</code>" in got
    assert "• todo: <code>bridge.py</code>" in got


def test_protects_code_from_other_rules():
    # bold внутри инлайн кода не должен преобразоваться
    text = "Use `**bold markdown**` syntax"
    got = fmt.format_for_telegram(text)
    assert "<code>**bold markdown**</code>" in got
    assert "<b>bold markdown</b>" not in got


def test_escapes_raw_html_from_agent():
    # агент случайно написал HTML — он должен быть экранирован
    got = fmt.format_for_telegram("<script>alert(1)</script>")
    assert "&lt;script&gt;" in got
    assert "<script>" not in got


# ───────────────── split ─────────────────

def test_split_short_text_single_chunk():
    assert fmt.split_for_telegram("short", limit=100) == ["short"]


def test_split_long_text_on_paragraphs():
    text = "para one line.\n\npara two line.\n\npara three line."
    chunks = fmt.split_for_telegram(text, limit=25)
    assert len(chunks) >= 2
    for c in chunks:
        assert len(c) <= 25


def test_split_no_newlines_falls_back_to_space():
    text = "word " * 50
    chunks = fmt.split_for_telegram(text.strip(), limit=30)
    assert len(chunks) > 1
    for c in chunks:
        assert len(c) <= 30


def test_split_empty_returns_empty_list_item():
    assert fmt.split_for_telegram("") == [""]
