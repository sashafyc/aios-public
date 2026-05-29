# Router — карта документации системы

Когда пользователь спрашивает «как это работает» или тебе нужно что-то настроить — читай нужный документ из `knowledge/system/`.

| Тема | Документ |
|---|---|
| Общая архитектура (мост, агенты, routing, state) | `knowledge/system/architecture.md` |
| Как создать агента | `knowledge/system/add-agent.md` |
| Runner'ы: claude/codex/deepseek/gemini, авторизация, стоимость, переключение | `knowledge/system/runners.md` |
| Делегация: теги, цепочки, иерархия | `knowledge/system/delegation.md` |
| Голосовые / транскрибация (AssemblyAI) | `knowledge/system/voice.md` |
| Cron / единое расписание (один файл `scripts/crontab`) | `knowledge/system/cron.md` |
| State machine: IDLE/ACTIVE/WAITING/BUSY, keep-alive, daily reset, compact | `knowledge/system/state-machine.md` |
| Траблшутинг: бот молчит, WAITING завис, форматирование, OAuth | `knowledge/system/troubleshooting.md` |
| Обновление системы: проверка, бэкап, откат, конфликты | `knowledge/system/update.md` |

## Обновление системы (новая версия)

Когда пользователь говорит «проверь обновления» / «есть ли новая версия» / «обнови систему»:

1. **Проверь:** `bash $AIOS_ROOT/scripts/update.sh --check`
   - exit 0 + «уже последняя версия» → сообщи что всё актуально, ничего не делай.
   - exit 0 + версия и changelog → есть обнова.
2. **Если обнова есть — спроси подтверждение** (ОБЯЗАТЕЛЬНО, никогда не обновляй молча):
   > Вышла версия **X** (что нового: …краткий changelog…). Установить? Я сделаю
   > бэкап, обновлю код, прогоню тесты и перезапущу мост. Твои настройки, ключи,
   > кастомные агенты и правки инструкций сохранятся.
3. **При согласии:** `bash $AIOS_ROOT/scripts/update.sh --yes`
4. **Доложи итог:** старая → новая версия, путь к бэкапу. Если скрипт сообщил о
   конфликтах или откате (тесты упали) — честно передай это пользователю.

Детали (что бэкапится, как откатиться, частые проблемы) — `knowledge/system/update.md`.

Проверка обновлений уже стоит в едином расписании (`scripts/crontab`, пн 09:00) —
её добавлять не нужно.

## Расписание (cron) — единый файл

Всё, что выполняется по расписанию (watchdog, doctor, чистка, проверка обновлений,
задачи агентов), живёт в ОДНОМ файле `$AIOS_ROOT/scripts/crontab`. Не ищи задачи по
разным местам.

- **Показать расписание:** `bash $AIOS_ROOT/scripts/cron-sync.sh --list`
- **Добавить задачу агенту:** допиши строку в раздел «Задачи агентов» файла
  `$AIOS_ROOT/scripts/crontab` по шаблону `trigger.py --agent <имя> --message "…" --isolated`,
  затем примени: `bash $AIOS_ROOT/scripts/cron-sync.sh`.
- `cron-sync.sh` ставит задачи в управляемый блок crontab и не трогает чужие строки.
- В cron НЕ добавляй daily-reset/keep-alive (это делает сам мост) и запуск самого моста.

Детали — `knowledge/system/cron.md`.

## Ключевые файлы системы

| Что | Путь (от AIOS_ROOT) |
|---|---|
| Конфиг агентов | `bridge/agents.toml` |
| Секреты | `bridge/.env` (chmod 600, не трогать руками без нужды) |
| Permissions | `bridge/settings.json` |
| Health check | `bridge/doctor.py` |
| Единое расписание (cron) | `scripts/crontab` + `scripts/cron-sync.sh` |
| Cron entry point (агент-сессии) | `bridge/trigger.py` |
| Обновление системы | `scripts/update.sh` (`--check` / `--yes`) |
| Бэкапы обновлений | `.backups/<timestamp>/` |
| Логи моста | `logs/bridge/` |
| Расход (cost/tokens) | `logs/sessions/*.jsonl` |
| Диалоги | `logs/conversations/*.jsonl` |
| Навык создания агента | `skills/create-agent/SKILL.md` |
