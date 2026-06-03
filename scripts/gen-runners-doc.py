#!/usr/bin/env python3
"""
gen-runners-doc.py — генерирует knowledge/system/agents-runners.md из bridge/agents.toml
(+ цены из bridge/pricing.py). Единственный автоген-артефакт по раннерам.

Источник правды: данные → agents.toml, цены → pricing.py, карта → agents-runners.md.
Вся остальная проза (runners.md, INDEX и т.п.) только ССЫЛАЕТСЯ на agents-runners.md.

Вызывается автоматически hook'ом при hot-reload agents.toml (в bridge/agents_registry.py).
Вручную: python3 scripts/gen-runners-doc.py [--check]   (--check — для CI, exit 1 если stale)

Пути берутся от корня репозитория (родитель папки scripts/) или из $AIOS_ROOT.
"""
from __future__ import annotations
import sys, os, tempfile, tomllib
from pathlib import Path

ROOT = Path(os.environ.get("AIOS_ROOT", "")) if os.environ.get("AIOS_ROOT") else Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bridge"))
try:
    import pricing as _pricing
except Exception:
    _pricing = None

# agents.toml пользователя (gitignored); fallback на трекаемый шаблон
TOML = ROOT / "bridge" / "agents.toml"
if not TOML.exists():
    TOML = ROOT / "bridge" / "agents.toml.example"
OUT_MD = ROOT / "knowledge" / "system" / "agents-runners.md"

RUNNER_LABEL = {
    "claude": "Claude CLI (claude_runner.py)",
    "codex": "Codex CLI (codex_runner.py)",
    "gemini": "Gemini CLI (gemini_runner.py)",
    "deepseek": "CodeWhale CLI (deepseek_runner.py)",
}


def _price(model):
    try:
        return _pricing.price_label(model) if _pricing else ""
    except Exception:
        return ""


def _atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".gentmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


def load_agents():
    d = tomllib.load(open(TOML, "rb"))
    rows = []
    for name, a in d.get("agents", {}).items():
        rows.append({
            "name": name,
            "display": a.get("display_name", name),
            "runner": a.get("runner_type", "?"),
            "model": a.get("model", "?"),
            "role": (a.get("role", "") or "")[:70],
        })
    return rows


def render(rows):
    engines = sorted({r["runner"] for r in rows})
    eng = "\n".join(f"- **{e}** → {RUNNER_LABEL.get(e, e)}" for e in engines)
    by = {}
    for r in rows:
        by.setdefault((r["runner"], r["model"]), []).append(r["name"])
    summary = "\n".join(
        f"- **{rn} / {ml}** ({len(v)}): {', '.join(sorted(v))} — {_price(ml)}"
        for (rn, ml), v in sorted(by.items())
    )
    tbl = ["| Агент | Раннер | Модель | Цена 1M (in/out) | Роль |",
           "|---|---|---|---|---|"]
    for r in sorted(rows, key=lambda x: (x["runner"], x["model"], x["name"])):
        tbl.append(f"| `{r['name']}` ({r['display']}) | {r['runner']} | `{r['model']}` | {_price(r['model'])} | {r['role']} |")
    table = "\n".join(tbl)
    return f"""# Агенты: раннеры, модели, цены (live)

> Тип: autogen · ЕДИНЫЙ ИСТОЧНИК по раннерам/моделям/ценам агентов.
> Генерируется из `bridge/agents.toml` (+ `bridge/pricing.py`) скриптом `scripts/gen-runners-doc.py`.
> Обновляется автоматически при hot-reload bridge. РЕДАКТИРОВАТЬ ВРУЧНУЮ НЕЛЬЗЯ.
> Сменить раннер = поменять `runner_type`/`model` в `agents.toml` → эта карта обновится сама.
> Модель агента и цена нигде больше не дублируются — только здесь.

## Движки (runner_type → реализация)

{eng}

## Сводка по моделям и ценам

{summary}

## Полная таблица

{table}

---
_Источник: `bridge/agents.toml` + `bridge/pricing.py`. Правки — только там._
"""


def main():
    rows = load_agents()
    content = render(rows)
    if "--check" in sys.argv:
        ok = OUT_MD.exists() and OUT_MD.read_text(encoding="utf-8") == content
        print("OK: agents-runners.md актуален" if ok else "STALE: прогони scripts/gen-runners-doc.py")
        sys.exit(0 if ok else 1)
    _atomic_write(OUT_MD, content)
    print(f"agents-runners.md обновлён ({len(rows)} агентов)")


if __name__ == "__main__":
    main()
