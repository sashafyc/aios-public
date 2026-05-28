# Router — карта документации системы

Когда пользователь спрашивает «как это работает» или тебе нужно что-то настроить — читай нужный документ из `knowledge/system/`.

| Тема | Документ |
|---|---|
| Общая архитектура (мост, агенты, routing, state) | `knowledge/system/architecture.md` |
| Как создать агента | `knowledge/system/add-agent.md` |
| Runner'ы: claude/codex/deepseek/gemini, авторизация, стоимость, переключение | `knowledge/system/runners.md` |
| Делегация: теги, цепочки, иерархия | `knowledge/system/delegation.md` |
| Голосовые / транскрибация (AssemblyAI) | `knowledge/system/voice.md` |
| Cron / запланированные задачи (trigger.py) | `knowledge/system/cron.md` |
| State machine: IDLE/ACTIVE/WAITING/BUSY, keep-alive, daily reset, compact | `knowledge/system/state-machine.md` |
| Траблшутинг: бот молчит, WAITING завис, форматирование, OAuth | `knowledge/system/troubleshooting.md` |

## Ключевые файлы системы

| Что | Путь (от AIOS_ROOT) |
|---|---|
| Конфиг агентов | `bridge/agents.toml` |
| Секреты | `bridge/.env` (chmod 600, не трогать руками без нужды) |
| Permissions | `bridge/settings.json` |
| Health check | `bridge/doctor.py` |
| Cron entry point | `bridge/trigger.py` |
| Логи моста | `logs/bridge/` |
| Расход (cost/tokens) | `logs/sessions/*.jsonl` |
| Диалоги | `logs/conversations/*.jsonl` |
| Навык создания агента | `skills/create-agent/SKILL.md` |
