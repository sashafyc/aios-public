"""
trigger.py — entry point для cron-расписаний.

Запускает нужное действие без поднятия полноценного bridge-loop'a.
Общается с живым bridge-процессом через файловый queue (простейший,
чтобы не тянуть Redis на старте).

Usage:
    python3 trigger.py daily_reset
    python3 trigger.py --agent tasks --message morning_review
    python3 trigger.py --agent priya --message "..." --isolated

Флаги:
    --agent <name>      кому триггерим
    --message "..."     текст сообщения (или preset-алиас, см. PRESETS)
    --isolated          одноразовый запуск без --resume (для cron)
    --announce          опционально прислать служебку в топик перед запуском
                        (зарезервировано, пока не реализовано)

Legacy-совместимость:
    Старая форма `trigger.py <agent> <message>` (позиционные) тоже работает.
    Используется только в не-cron случаях; cron должен идти через флаги и
    обязательно с --isolated.

Как работает:
    Записывает JSON-файл в {AIOS_ROOT}/bridge/queue/ c action + payload.
    Основной bridge-процесс опрашивает эту папку и выполняет команду
    в своём event loop'e.

NB: очередь реализована как files-dir а не SQLite чтобы не плодить схемы
и чтобы файл сам по себе был идемпотентным артефактом для дебага.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from env_loader import load_env
load_env()

from path_config import queue_dir

QUEUE_DIR = queue_dir()


PRESETS = {
    "morning_review": "Пробежись по активным задачам и напиши короткий статус: что сдвинулось со вчера, что в WAITING, где нужен мой вход.",
    "daily_scan": "Сделай утренний скан своей зоны ответственности и положи саммари в этот топик.",
    "daily_check": "Проверь состояние своей зоны ответственности и напиши что видишь.",
    "health": "Хелсчек системы: диск, память, статус bridge, странности в логах за час.",
}


def enqueue(action: str, **payload) -> Path:
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    fname = f"{ts}-{action}-{int(time.time()*1000)%1000:03d}.json"
    path = QUEUE_DIR / fname
    path.write_text(
        json.dumps({"action": action, "ts": ts, **payload}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def resolve_message(raw: str) -> str:
    """Алиас из PRESETS → развёрнутый текст. Иначе — raw как есть."""
    return PRESETS.get(raw, raw)


def run_flagged(args: argparse.Namespace) -> int:
    if args.daily_reset:
        enqueue("daily_reset")
        print("enqueued daily_reset")
        return 0

    if not args.agent or not args.message:
        print("error: --agent и --message обязательны (или используй --daily-reset)")
        print(__doc__)
        return 1

    msg = resolve_message(args.message)
    payload = {"agent": args.agent, "text": msg}
    if args.isolated:
        payload["isolated"] = True
    if args.session_id:
        payload["session_id"] = args.session_id
    enqueue("user_message", **payload)
    label = "isolated" if args.isolated else "main"
    print(f"enqueued {label} message for {args.agent}")
    return 0


def run_positional(argv: list[str]) -> int:
    """Legacy: `trigger.py daily_reset` или `trigger.py <agent> <message_or_preset>`."""
    if len(argv) < 2:
        print(__doc__)
        return 1

    cmd = argv[1]

    if cmd == "daily_reset":
        enqueue("daily_reset")
        print("enqueued daily_reset")
        return 0

    if len(argv) >= 3:
        agent = cmd
        rest = " ".join(argv[2:])
        msg = resolve_message(rest)
        enqueue("user_message", agent=agent, text=msg)
        print(f"enqueued main message for {agent}")
        return 0

    print(__doc__)
    return 1


def main(argv: list[str]) -> int:
    # Если первый аргумент начинается с '-' — считаем это argparse-режимом.
    # Иначе — legacy-positional (обратная совместимость).
    if len(argv) >= 2 and argv[1].startswith("-"):
        parser = argparse.ArgumentParser(description="Bridge v8 cron trigger", add_help=True)
        parser.add_argument("--agent", type=str, default=None, help="Имя агента")
        parser.add_argument("--message", type=str, default=None, help="Текст или preset-алиас")
        parser.add_argument("--isolated", action="store_true",
                            help="Одноразовый запуск без --resume (для cron)")
        parser.add_argument("--session-id", dest="session_id", type=str, default=None,
                            help="Возобновить конкретную сессию (uuid)")
        parser.add_argument("--announce", action="store_true",
                            help="(reserved) служебка в топик перед запуском")
        parser.add_argument("--daily-reset", dest="daily_reset", action="store_true",
                            help="Выполнить daily_reset всех агентов")
        args = parser.parse_args(argv[1:])
        return run_flagged(args)

    return run_positional(argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
