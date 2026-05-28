# Траблшутинг

## Бот не отвечает вообще

1. Мост запущен? `python3 bridge/doctor.py` → строка lockfile / systemd.
2. BOT_TOKEN правильный? doctor покажет `@username` если ок.
3. Бот — админ группы? Зайди в настройки группы → Admins → бот должен быть там.
4. Group Privacy выключен? @BotFather → /mybots → Bot Settings → Group Privacy → OFF (иначе бот не видит сообщения в группе).
5. GROUP_CHAT_ID совпадает с реальной группой? Проверь .env / agents.toml.
6. Рестарт моста.

## Бот отвечает не в том топике / молчит в топике

- topic_id в agents.toml совпадает с реальным топиком? Перешли сообщение из топика @raw_data_bot → поле `message_thread_id`.

## Агент завис в WAITING

- `/state` в топике → посмотри `waiting_for`. Если делегат не ответил — `/new` сбросит.

## context_overflow / агент тупит

- Контекст переполнен. `/new` в топике агента → сброс сессии.

## rate_limit

- Провайдер ограничил. Подожди до :00/:30 (мост сам разблокирует) или переключи агента на другой runner (runners.md).

## auth / token expired

- CLI-сессия протухла. Перелогинься: `claude auth` / `codex auth` / `gemini auth`. На VPS — под пользователем aios: `su - aios -c "claude auth"`.

## Форматирование в TG поехало

- Агент пишет HTML руками вместо Markdown. Напомни ему `_shared/tg-format.md` (пиши Markdown, не HTML).

## Диск заполнен

- Почисти `workspace/temp/`. Или дождись месячного cleanup (scripts/maintenance/cleanup_temp.py).

## Файл не пришёл в чат

- Агент забыл тег `[FILE:/path]`, или путь вне AIOS_ROOT//tmp (whitelist). Проверь логи: `logs/bridge/bridge.log`.

## Где смотреть логи

| | |
|---|---|
| Общий лог | `logs/bridge/bridge.log` |
| Только ошибки | `logs/bridge/errors.log` |
| Диалоги | `logs/conversations/*.jsonl` |
| Расход (cost/tokens) | `logs/sessions/*.jsonl` |
