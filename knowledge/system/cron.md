# Cron — запланированные задачи

Агент может получать задачи по расписанию (утренний отчёт, ежедневная проверка).

## Как работает

`bridge/trigger.py` кладёт JSON-файл в `bridge/queue/`. Мост опрашивает папку и выполняет задачу в своём event loop'е — без поднятия второго процесса.

## Изолированные сессии

Флаг `--isolated` = одноразовый запуск без `--resume`. Cron-задача НЕ ломает основную сессию агента (она идёт в отдельной сессии).

## Примеры запуска

```bash
# Утренний отчёт агенту (preset)
python3 bridge/trigger.py --agent sysadmin --message morning_review --isolated

# Произвольное сообщение
python3 bridge/trigger.py --agent assistant --message "Проверь почту" --isolated

# Daily reset вручную
python3 bridge/trigger.py daily_reset
```

## Готовые пресеты

`morning_review`, `daily_scan`, `daily_check`, `health` (см. PRESETS в trigger.py).

## Настройка cron (Linux)

```bash
crontab -e
# Каждое утро в 9:00 — отчёт Сисадмина:
0 9 * * * cd /home/aios/aios-public && python3 bridge/trigger.py --agent sysadmin --message morning_review --isolated
```

На Mac — `launchd` или `cron` аналогично.

Через Сисадмина: скажи «настрой ежедневный отчёт в 9 утра» — добавит строку в crontab.
