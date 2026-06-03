"""
pricing.py — центральный прайс-лист моделей ($/1M токенов).
ЕДИНСТВЕННЫЙ источник правды по ценам. Используется:
  - раннерами (deepseek_runner, codex_runner) для расчёта cost_usd;
  - session_logger / daily-сводкой как fallback, если раннер не дал cost;
  - scripts/gen-runners-doc.py — показать цену в карте раннеров.

claude_runner берёт точный cost из CLI (`total_cost_usd`, учитывает кэш) —
для него прайс тут справочный.

Обновлять цены ТОЛЬКО здесь.
"""
from __future__ import annotations

# $ за 1,000,000 токенов
PRICING: dict[str, dict] = {
    # Anthropic (Max-подписка — реальный cost берётся из claude CLI total_cost_usd;
    # значения тут справочные, по публичному API-прайсу)
    "claude-sonnet-4-6": {"input": 3.0,  "output": 15.0, "billing": "subscription"},
    "claude-opus-4-6":   {"input": 15.0, "output": 75.0, "billing": "subscription"},
    # OpenAI Codex (Plus-подписка; API-прайс справочно)
    "gpt-5.4":           {"input": 1.25, "output": 10.0, "billing": "subscription"},
    "gpt-5.4-codex":     {"input": 1.25, "output": 10.0, "billing": "subscription"},
    "gpt-5.5-codex":     {"input": 1.25, "output": 10.0, "billing": "subscription"},
    # DeepSeek (pay-as-you-go — РЕАЛЬНЫЕ деньги)
    "deepseek-v4-pro":   {"input": 0.435, "output": 0.87, "billing": "payg"},
    "deepseek-v4-flash": {"input": 0.14,  "output": 0.28, "billing": "payg"},
    # Gemini (free tier)
    "gemini":            {"input": 0.0,  "output": 0.0,  "billing": "free"},
}

DEFAULT = {"input": 0.0, "output": 0.0, "billing": "unknown"}


def price_for(model: str) -> dict:
    return PRICING.get(model, DEFAULT)


def cost_usd(model: str, tokens_in: int, tokens_out: int) -> float:
    """Стоимость прогона по числу токенов и модели."""
    p = price_for(model)
    return (tokens_in or 0) * p["input"] / 1e6 + (tokens_out or 0) * p["output"] / 1e6


def price_label(model: str) -> str:
    """Человекочитаемая цена для документации, напр. '$0.44 / $0.87 за 1M (pay-as-you-go)'."""
    p = price_for(model)
    if p["billing"] == "free":
        return "бесплатно (free tier)"
    if p["billing"] == "subscription":
        return f"подписка (~${p['input']:.2f}/${p['output']:.2f} за 1M по API-прайсу)"
    return f"${p['input']:.2f} / ${p['output']:.2f} за 1M (pay-as-you-go)"
