# Аудит aios-public

> Дата: 2026-05-29  
> Репозиторий: `sashafyc/aios-public`  
> Статус: `DONE_WITH_CONCERNS`

## Короткий вывод

Проект уже выглядит как рабочий продукт, а не как сырой черновик: есть bridge, агенты, установщик, doctor, watchdog, документация и тесты. Базовые тесты проходят: `81 passed`.

Главный риск публичного релиза не в найденных утечках ключей, а в модели доверия: любой участник Telegram-группы может взаимодействовать с агентами, а агенты запускаются в bypass/yolo-режиме и местами имеют доступ к системным операциям.

Перед активным публичным промо нужен короткий hardening sprint.

## Что проверено

- Публичная Git-поверхность: отслеживаемые файлы, `.gitignore`, история последних коммитов.
- Поиск секретов и приватных хвостов.
- Установщик `install.sh`.
- Автозапуск и watchdog.
- Bridge core: routing, queue, Telegram polling, callbacks, files, sessions, runners.
- Инструкции агентов: Ассистент, Сисадмин, Скрайбер.
- Документация `README.md`, `docs/INSTALL.md`, `docs/CONFIG.md`.
- Тесты.

## Хорошее

- Архитектура понятная и сильная для MVP: один Telegram-бот, много топиков, каждый топик как агент.
- Есть реальные продуктовые фичи: hot-reload агентов, делегация, streaming, keep-alive, daily reset, doctor, watchdog.
- Есть тесты на важные части: formatter, delegation router, session manager, product config.
- `bridge/.env` не отслеживается Git, `.gitignore` закрывает основные runtime-файлы.
- Прямых API-ключей или токенов в отслеживаемых файлах не найдено.
- Роли трёх агентов понятны: Ассистент как хаб, Сисадмин как self-service управление, Скрайбер как пример узкого агента.

## Критичные проблемы

### 1. Нет allowlist владельца Telegram-группы

Файл: `bridge/tg_bridge.py:1568`

Bridge принимает сообщения от любого небота внутри разрешённого `chat_id`. Callback-кнопки `/new` и approvals тоже может нажать любой участник группы.

Почему опасно:

- Любой участник группы может управлять агентами.
- Агенты запускаются с bypass/yolo-правами.
- Через Сисадмина можно менять конфиги и потенциально трогать секреты.
- Inline approval теряет смысл, если нажать кнопку может любой человек в группе.

Что сделать:

- Добавить `OWNER_USER_ID` и `ALLOWED_USER_IDS` в `.env`.
- Фильтровать `message.from_user.id`.
- Фильтровать `callback.from_user.id`.
- Для публичного продукта сделать это обязательным шагом установки.

### 2. Bypass/yolo-режим включён по умолчанию

Файлы:

- `bridge/claude_runner.py:172`
- `bridge/codex_runner.py:59`
- `bridge/gemini_runner.py:71`
- `bridge/settings.json:5`

Claude/Codex/Gemini запускаются в режиме, где approvals и sandbox максимально ослаблены.

Почему опасно:

- Prompt injection из Telegram превращается в реальные действия в файловой системе.
- Ошибка агента может привести к повреждению установки.
- Для open-source аудитории это будет красный флаг.

Что сделать:

- По умолчанию включить safe mode.
- Bypass/yolo оставить только как явно включаемую опцию `AIOS_UNSAFE_BYPASS=1`.
- В README явно объяснить риск.

### 3. Опциональный sudoers для apt/dnf слишком широкий

Файл: `install.sh:325`

Установщик может создать правило:

```text
NOPASSWD: apt-get update, apt-get install -y *, apt-get install *
```

Почему опасно:

- `apt-get install` с wildcard-аргументами и опциями может стать путём к root-level abuse.
- Агент, управляемый текстом из Telegram, получает слишком широкую системную власть.

Что сделать:

- Убрать эту опцию из v1 публичного релиза.
- Или заменить на строгий allowlist пакетов: `ffmpeg`, `tesseract-ocr`, `libreoffice` и т.п.
- Лучше: Сисадмин предлагает команду пользователю, пользователь сам выполняет.

### 4. `tg_bridge.py` не загружает `.env` при manual/mac запуске

Файлы:

- `bridge/tg_bridge.py:14`
- `bridge/doctor.py:25`
- `scripts/enable.sh:78`
- `docs/INSTALL.md:67`

`doctor.py` сам читает `bridge/.env`, а `tg_bridge.py` нет. Linux systemd подхватывает `.env` через `EnvironmentFile`, но manual запуск и macOS launchd могут стартовать без токенов.

Что сделать:

- Вынести `.env` loader в общий модуль и вызывать в `tg_bridge.py`, `doctor.py`, `trigger.py`.
- В launchd plist прописать env или запускать через wrapper.
- Обновить docs: manual-команда должна явно грузить `.env`.

### 5. `BUSY` состояние не используется

Файлы:

- `bridge/session_manager.py:176`
- `bridge/tg_bridge.py:1875`

Состояние `BUSY` описано и проверяется, но bridge нигде не вызывает `sessions.on_busy(...)` вокруг runner calls.

Почему опасно:

- `/new` может сбросить сессию во время активного tool call.
- Daily reset может считать агента не busy.
- Состояния в UI/doctor могут врать.

Что сделать:

- В `_run_agent_actual` ставить `on_busy(agent, True)` перед runner call.
- В `finally` возвращать `on_busy(agent, False)` с учётом `WAITING`.
- Добавить тест на запрет reset во время BUSY.

## Высокий приоритет

### 6. Отправка файлов разрешает слишком широкие пути

Файл: `bridge/tg_bridge.py:1024`

Сейчас `[FILE:]` может отправлять файлы из всего `AIOS_ROOT` и `/tmp`, а проверка сделана через `str(...).startswith(...)`.

Риски:

- Сисадмин с доступом к `.env` теоретически может отправить секреты пользователю.
- `/tmp` слишком широкий для multi-user Linux.
- `startswith` лучше заменить на `Path.is_relative_to`.

Что сделать:

- Разрешить только `workspace/temp/<agent>/`, `workspace/permanent/exports/` и безопасный temp dir.
- Явно запретить `bridge/.env`, `.state`, `logs/`, credentials.
- Использовать `resolved.is_relative_to(root)`.

### 7. Имена входящих файлов не санитизируются

Файл: `bridge/tg_bridge.py:1402`

Telegram `file_name` кладётся в путь напрямую.

Что сделать:

- Использовать `Path(filename).name`.
- Заменять опасные символы.
- Ограничить длину имени.
- Добавить лимит размера файла и количества файлов в одном сообщении.

### 8. DeepSeek теряет контекст после рестарта

Файл: `bridge/deepseek_runner.py:49`

История DeepSeek хранится только в памяти. После рестарта `.state` может содержать fake session id, но реального контекста уже нет.

Что сделать:

- Либо сохранять историю в `.state`/отдельный JSONL.
- Либо честно сбрасывать session id для API-runner при рестарте.
- В docs объяснить отличие CLI-runners и API-runners.

## Средний приоритет

### 9. `tg_bridge.py` слишком большой и содержит production-хвосты

Файл: `bridge/tg_bridge.py`

Файл на 2000+ строк держит Telegram polling, routing, queue, sessions, files, transcription, approvals, maintenance и reset. Внутри остались `personal_`, `/root`, v9-комментарии, ссылки на internal materials.

Что сделать:

- Разделить на модули:
  - `telegram_poller.py`
  - `message_router.py`
  - `file_io.py`
  - `maintenance.py`
  - `approvals.py`
  - `bridge_core.py`
- Убрать production-only ветки из public версии.
- Обновить комментарии под v1 public, без ссылок на internal paths.

### 10. Нет CI и security checks

Сейчас тесты есть, но нет GitHub Actions.

Что сделать:

- Добавить CI:
  - `pytest`
  - `python -m compileall`
  - `ruff`
  - secret scan (`gitleaks` или `trufflehog`)
  - dependency audit (`pip-audit`)

### 11. README обещает установку одной командой, но URL placeholder

Файл: `README.md:10`

Сейчас:

```bash
curl -fsSL https://раздаётся-позже/install | bash
```

Что сделать:

- Заменить на реальный raw GitHub URL.
- Добавить короткое предупреждение: ставить только в приватную Telegram-группу.
- Добавить GIF/скринкаст работы.

### 12. Установщик пишет `.env` без escaping значений

Файл: `install.sh:348`

Если токен/ключ/путь содержит спецсимволы, `.env` может стать некорректным.

Что сделать:

- Генерировать `.env` через безопасный quote-функционал.
- Проверять `GROUP_CHAT_ID` и `topic_id` как числа.
- Валидировать формат Telegram bot token.

## Агенты

### Ассистент

Сильный универсальный агент-хаб. Хорошо, что ему явно сказано сохранять файлы в `workspace/temp/assistant/` и отправлять `[FILE:]`.

Что улучшить:

- Жёстче запретить системные действия: установка пакетов, правка `.env`, рестарты.
- Добавить инструкцию проверять размер/тип пользовательских файлов.
- Длинные ответы лучше не всегда сохранять в файл после 1000 символов: для Telegram это слишком низкий порог, продуктово может раздражать.

### Сисадмин

Хорошая идея продукта: пользователь управляет системой диалогом. Но это самый опасный агент.

Что улучшить:

- Сисадмин должен требовать подтверждение на любые изменения `.env`, sudoers, systemd, удаление агентов, установку системных пакетов.
- Сисадмин не должен сам отправлять секреты через `[FILE:]`.
- В prompt добавить правило: никогда не показывать токены целиком, только masked.
- Добавить отдельный `safe_sysadmin` режим для публичной установки.

### Скрайбер

Хороший пример узкого агента. Роль понятная, демонстрирует паттерн специализации.

Что улучшить:

- Сейчас bridge уже транскрибирует voice/audio/video_note через AssemblyAI до передачи агенту, а Скрайбер в prompt тоже думает, что сам делает весь pipeline. Нужно синхронизировать поведение.
- Добавить явное поведение при отсутствии `ASSEMBLYAI_API_KEY`: коротко объяснить, как включить.
- YouTube-flow лучше вынести в отдельный проверенный скрипт/skill, чтобы агент не импровизировал.

## Продуктовые улучшения

### Минимальный safe onboarding

Первому пользователю нужно дать не только install, но и безопасный путь:

- приватная группа;
- owner user id;
- safe mode;
- один тестовый агент;
- `doctor` после установки;
- первое сообщение "привет" и `/state`.

### Public demo mode

Сделать режим, где:

- нет доступа к `.env`;
- нет sudo;
- нет записи вне `workspace/temp`;
- можно только отвечать, читать загруженные файлы и создавать артефакты.

Это резко снизит страх у людей запускать проект.

### Product packaging

Что добавить в репозиторий:

- `SECURITY.md`
- `CONTRIBUTING.md`
- `docs/SAFE_MODE.md`
- `docs/THREAT_MODEL.md`
- demo GIF
- real install URL
- "Known limitations"

## Рекомендованный hardening sprint

Порядок действий:

1. Owner allowlist для сообщений и callback’ов.
2. Safe mode по умолчанию.
3. Убрать или сильно сузить sudoers для package install.
4. Общий `.env` loader для bridge/doctor/trigger/mac launchd.
5. Реальное `BUSY` состояние вокруг runner calls.
6. Сузить `[FILE:]` allowlist до export/temp директорий.
7. Санитизировать входящие filename и добавить лимиты файлов.
8. Добавить GitHub Actions: tests, compileall, ruff, secret scan.
9. Почистить production-хвосты из public-кода.
10. Обновить README и INSTALL под реальный публичный URL.

## Итоговый статус

`DONE_WITH_CONCERNS`

Проект рабочий и сильный как демонстрация AI-команды в Telegram. Но перед широкой публичной раздачей надо закрыть security hardening, иначе главный риск будет не "не заведётся", а "заведётся слишком мощно и слишком доверчиво".
