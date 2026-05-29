"""
Тесты парсера тегов delegation_router.py.

Запуск:
    cd aios-public && pytest tests/ -v
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))

import delegation_router as dr
import agents_registry


# ───────── code-block immunity ─────────


def test_tag_in_code_block_ignored():
    """Тег внутри ```...``` — пример документации, не команда."""
    text = (
        "Пример использования:\n"
        "```\n"
        "[DELEGATE:research] Собери базу\n"
        "[/DELEGATE]\n"
        "```\n"
        "А теперь реальная команда:\n"
        "[DELEGATE:research] Реальная задача"
    )
    tags = dr.parse(text)
    assert len(tags) == 1
    assert tags[0].body == "Реальная задача"


def test_tag_in_inline_backticks_parsed():
    """
    Тег в одинарных backticks ВСЁ РАВНО парсится как валидный.
    Агенты часто случайно оборачивают и это не должно ломать delegation.
    """
    text = "вот результат работы.\n\n`[RESULT:tasks:task-001]`"
    tags = dr.parse(text)
    assert len(tags) == 1
    assert tags[0].tag == "RESULT"
    assert tags[0].target_agent == "tasks"
    assert tags[0].task_id == "task-001"


def test_real_tag_still_works_with_code_block_nearby():
    """Рядом с code block настоящий тег должен работать."""
    text = (
        "Нашёл решение:\n"
        "```python\n"
        "print('hello')\n"
        "```\n"
        "\n"
        "[RESULT:tasks:task-42]\n"
        "Готово, см. выше.\n"
        "[/RESULT]"
    )
    tags = dr.parse(text)
    assert len(tags) == 1
    assert tags[0].tag == "RESULT"
    assert tags[0].target_agent == "tasks"
    assert tags[0].task_id == "task-42"


def test_strip_tags_removes_wrapping_backticks():
    """
    Если тег был обёрнут в инлайн-бэктики, strip_tags должен снять и их,
    чтобы в clean_text не остался висящий ` перед служебным сообщением.
    """
    text = "Собрал базу, смотри выше.\n\n`[RESULT:tasks:task-001]`"
    tags = dr.parse(text)
    clean = dr.strip_tags(text, tags)
    assert "`" not in clean
    assert "Собрал базу" in clean


def test_result_with_empty_body_uses_clean_text():
    """
    Fallback: если агент написал [RESULT:tasks:id] без тела (или в
    backticks в конце ответа) — router должен использовать весь
    clean_text как содержимое результата, чтобы он дошёл до заказчика.
    """
    router = dr.DelegationRouter(agents_registry)
    response = (
        "Собрал базу: 3 компании.\n"
        "iGooods, Ozon Fresh, Магнит Доставка.\n"
        "Файл: /workspace/temp/research/task-003.md\n\n"
        "`[RESULT:tasks:task-003]`"
    )
    result = router.handle("research", response)
    assert len(result["results"]) == 1
    r = result["results"][0]
    assert r["to"] == "tasks"
    assert r["task_id"] == "task-003"
    # body был пустой — должны получить весь clean_text
    assert "iGooods" in r["text"]
    assert "task-003.md" in r["text"]


# ───────── parser ─────────


def test_single_line_delegate():
    text = "[DELEGATE:research] Собери базу B2C маркетплейсов"
    tags = dr.parse(text)
    assert len(tags) == 1
    t = tags[0]
    assert t.tag == "DELEGATE"
    assert t.target_agent == "research"
    assert "Собери" in t.body


def test_multiline_delegate():
    text = (
        "Принял задачу.\n"
        "[DELEGATE:research:task-42]\n"
        "Собери 50 B2C маркетплейсов РФ.\n"
        "ICP: 100М+, без топ-5.\n"
        "[/DELEGATE]\n"
        "Жду результат."
    )
    tags = dr.parse(text)
    assert len(tags) == 1
    t = tags[0]
    assert t.tag == "DELEGATE"
    assert t.target_agent == "research"
    assert t.task_id == "task-42"
    assert "маркетплейсов" in t.body
    assert "ICP" in t.body


def test_waiting_for_multiple():
    text = "[WAITING_FOR:research:base-x, scout:probiv-y]"
    tags = dr.parse(text)
    keys = dr.extract_waiting_for(tags)
    assert "research:base-x" in keys
    assert "scout:probiv-y" in keys


def test_result_tag():
    text = "[RESULT:tasks:task-42]\n48 компаний готово.\n[/RESULT]"
    tags = dr.parse(text)
    results = dr.extract_results(tags)
    assert len(results) == 1
    assert results[0].target_agent == "tasks"
    assert results[0].task_id == "task-42"
    assert "48" in results[0].body


def test_ask_user_and_approval():
    text = "Нужно подтверждение. [ASK_USER] Какой бюджет? [APPROVAL_NEEDED]"
    tags = dr.parse(text)
    assert dr.has_ask_user(tags)
    assert dr.has_approval(tags)


def test_strip_tags():
    text = "Привет. [STATUS] Всё ок.\nЗавтра продолжу. [DELEGATE:research] сделай X"
    tags = dr.parse(text)
    clean = dr.strip_tags(text, tags)
    assert "[STATUS]" not in clean
    assert "[DELEGATE" not in clean
    assert "Привет." in clean
    assert "Завтра продолжу." in clean


def test_unknown_tag_ignored():
    text = "[FOOBAR:abc] этого тега не существует"
    tags = dr.parse(text)
    assert tags == []


def test_empty_input():
    assert dr.parse("") == []
    assert dr.parse("просто текст без тегов") == []


# ───────── DelegationRouter (hierarchy) ─────────


def test_router_allows_assistant_to_scriber():
    r = dr.DelegationRouter(agents_registry)
    out = r.handle("assistant", "[DELEGATE:scriber] Транскрибируй это")
    assert len(out["delegations"]) == 1
    assert out["delegations"][0]["to"] == "scriber"


def test_router_forbids_scriber_to_assistant():
    r = dr.DelegationRouter(agents_registry)
    # Скрайбер не имеет can_delegate_to — делегация запрещена
    out = r.handle("scriber", "[DELEGATE:assistant] что-то")
    assert out["delegations"] == []


def test_router_forbids_assistant_to_unknown():
    r = dr.DelegationRouter(agents_registry)
    # assistant может делегировать только scriber — не sysadmin
    out = r.handle("assistant", "[DELEGATE:sysadmin] сделай это")
    assert out["delegations"] == []


def test_router_clean_text_without_tags():
    r = dr.DelegationRouter(agents_registry)
    out = r.handle("assistant", "Всё сделано. [STATUS] работаю дальше.")
    assert "[STATUS]" not in out["clean_text"]
    assert "Всё сделано." in out["clean_text"]


def test_router_waiting_state():
    r = dr.DelegationRouter(agents_registry)
    out = r.handle(
        "assistant",
        "[DELEGATE:scriber:base-x] Транскрибируй\n[WAITING_FOR:scriber:base-x]",
    )
    assert out["waiting_for"] == ["scriber:base-x"]
    assert len(out["delegations"]) == 1


def test_router_restart_tag():
    r = dr.DelegationRouter(agents_registry)
    out = r.handle("sysadmin", "Сохранил ключ. [RESTART] добавил DEEPSEEK_API_KEY")
    assert out["restart_reason"] == "добавил DEEPSEEK_API_KEY"
    assert "[RESTART]" not in out["clean_text"]
    assert "Сохранил ключ." in out["clean_text"]


def test_router_no_restart_by_default():
    r = dr.DelegationRouter(agents_registry)
    out = r.handle("assistant", "Просто текст без тегов.")
    assert out["restart_reason"] is None
