# Справка по конфигурации

## bridge/.env — секреты и настройки

| Переменная | Обязательна | Что |
|---|---|---|
| `BOT_TOKEN` | да | Токен Telegram-бота (@BotFather) |
| `AIOS_GROUP_CHAT_ID` | да | ID группы (-100...). Приоритет над agents.toml |
| `AIOS_ROOT` | да | Абсолютный путь установки |
| `ALLOWED_USER_IDS` | опц. | Белый список user_id через запятую. Пусто = любой участник группы |
| `DEEPSEEK_API_KEY` | для deepseek | Ключ DeepSeek API |
| `OPENAI_API_KEY` | опц. | Для codex в API-режиме |
| `ASSEMBLYAI_API_KEY` | опц. | Голосовые + Скрайбер |
| `TIMEZONE_OFFSET_HOURS` | нет (=3) | Смещение от UTC |
| `DAILY_RESET_HOUR` / `_MINUTE` | нет (6/30) | Время ежедневного сброса сессий |
| `WATCHDOG_TOPIC_ID` | нет | Топик для алертов watchdog (обычно Сисадмин) |
| `MAX_INCOMING_FILE_MB` | нет (=20) | Лимит размера входящего файла |
| `MAX_FILES_PER_MESSAGE` | нет (=10) | Лимит файлов в одном сообщении |
| `TG_API_BASE` | нет | Кастомный Telegram API (прокси) |

## bridge/agents.toml — агенты

`[global]`: `aios_root`, `group_chat_id`.

`[agents.<name>]`:

| Поле | Что |
|---|---|
| `display_name` | Имя в логах |
| `topic_id` | ID топика в группе |
| `bot_token_env` | Имя env с токеном (обычно `BOT_TOKEN`) |
| `workdir` | Папка агента (от AIOS_ROOT) |
| `model` | Модель runner'а |
| `runner_type` | claude / codex / deepseek / gemini |
| `role` | Краткая роль |
| `stream` | true для claude/deepseek |
| `can_delegate_to` | Список агентов для делегации |
| `timeout_s` | Таймаут запроса (1800 по умолч.) |
| `enabled` | Включён (true по умолч.) |

Правки `agents.toml` подхватываются автоматически (hot-reload, ~60с). Правки `.env` требуют рестарта моста.

## bridge/settings.json — permissions

Нативная схема Claude Code: `permissions.deny` (жёсткий запрет) и `permissions.ask` (спросить) с паттернами вроде `Bash(rm -rf:*)`, `Read(.env)`, `Read(./.git/**)`. Эти правила действуют **даже в bypass-режиме** — это и есть страховка от опасных команд и чтения секретов. `settings-sysadmin.json` мягче (Сисадмин управляет ключами, поэтому не запрещает `.env`).

## Запуск/остановка

```bash
# вручную
.venv/bin/python bridge/tg_bridge.py

# через автозапуск
bash scripts/enable.sh                    # включить
systemctl restart aios-bridge             # Linux
launchctl unload ~/Library/LaunchAgents/com.aios-public.bridge.plist   # Mac: стоп
```
