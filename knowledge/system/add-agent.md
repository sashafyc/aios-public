# Как добавить агента

Это делает Сисадмин через навык `create-agent` (skills/create-agent/SKILL.md). Здесь — справка по механике.

> Новый агент по шаблону автоматически получает: чтение общих протоколов (`_shared/`), правило «сначала ищи в базе знаний», эскалацию ошибок к Сисадмину. Не убирай эти секции.

## Минимальные шаги

1. **Папка** `agents/<name>/` (латиница, lowercase: researcher, cook, marketer).
2. **CLAUDE.md** — роль агента (за основу `agents/_template/CLAUDE.md` — там уже зашиты база знаний и эскалация).
3. **context.md + journal.md** — из `agents/_template/`.
4. **Симлинки:**
   ```bash
   cd agents/<name> && ln -sf CLAUDE.md AGENTS.md && ln -sf CLAUDE.md GEMINI.md
   ```
5. **Топик в TG** — создаёт пользователь, присылает ссылку/пересланное сообщение → берёшь `message_thread_id`.
6. **agents.toml** — секция:
   ```toml
   [agents.<name>]
   display_name = "<Имя>"
   topic_id = <topic_id>
   bot_token_env = "BOT_TOKEN"
   workdir = "agents/<name>"
   model = "claude-sonnet-4-6"
   runner_type = "claude"
   role = "<краткая роль>"
   stream = true
   ```
7. Hot-reload подхватит за 60 сек.
8. Проверка: `python3 bridge/doctor.py`.

## Поля agents.toml

| Поле | Что |
|---|---|
| `display_name` | Имя в логах/служебках |
| `topic_id` | ID топика в группе |
| `bot_token_env` | Имя env-переменной с токеном бота |
| `workdir` | Папка агента (относительно AIOS_ROOT) |
| `model` | Модель runner'а |
| `runner_type` | claude / codex / deepseek / gemini |
| `role` | Краткое описание роли |
| `stream` | Стримить ответ в TG (claude/deepseek: true) |
| `can_delegate_to` | Список агентов, кому может делегировать |
| `timeout_s` | Таймаут на запрос (по умолч. 1800) |
| `enabled` | Включён ли (по умолч. true) |

## Удаление агента

1. Убрать секцию из agents.toml.
2. Переместить папку в `agents/_archive/<name>/`.
3. Hot-reload подхватит.
