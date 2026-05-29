# Установка вручную

Если `install.sh` не подошёл (или хочешь понимать что происходит) — поставь руками. Это те же шаги, что делает установщик.

> **AI-агенту (Claude Code / Codex):** ставь именно по этой инструкции, а НЕ через `install.sh` — установщик интерактивный (читает ответы из терминала) и не рассчитан на автоматический прогон. Шаги ниже выполняй по порядку. Создать Telegram-бота (@BotFather) и группу с топиками может только человек — на шаге 3 остановись и попроси у пользователя `BOT_TOKEN`, `chat_id` группы и три `topic_id`. Остальное (клон, venv, зависимости, конфиги, материализация, автозапуск) сделай сам, в конце прогони `doctor.py` и убедись, что 0 errors.

## Требования

- Python **3.11+** (нужен `tomllib`)
- `pip`, `venv`
- LLM CLI на выбор: `claude` / `codex` / `gemini` (или DeepSeek API key)
- Опционально: `ffmpeg`, `yt-dlp` (для Скрайбера)
- Linux/macOS/WSL2. На VPS под root — заведи отдельного пользователя (см. ниже).

## 1. Получить код

```bash
git clone https://github.com/sashafyc/aios-public ~/aios-public
cd ~/aios-public
```

## 2. Виртуальное окружение

```bash
python3 -m venv .venv
.venv/bin/pip install -r bridge/requirements.txt
.venv/bin/pip install yt-dlp        # для YouTube в Скрайбере
```

## 3. Telegram: группа + бот

**Группа:** создай супергруппу, включи Topics, сделай 3 топика (Ассистент / Система / Скрайбер).
Узнай `chat_id` и `message_thread_id` каждого топика через @raw_data_bot (перешли ему сообщение из топика).

**Бот:** @BotFather → `/newbot` → возьми токен. Затем Bot Settings → Allow Groups **ON**, Group Privacy **OFF**. Добавь бота в группу **админом**.

## 4. Конфиг

Создай `bridge/.env` (chmod 600):
```bash
cp bridge/.env.example bridge/.env
chmod 600 bridge/.env
# заполни BOT_TOKEN, AIOS_GROUP_CHAT_ID, AIOS_ROOT (=абсолютный путь установки)
```

Создай конфиг агентов из шаблона и пропиши значения:
```bash
cp bridge/agents.toml.example bridge/agents.toml
# в bridge/agents.toml укажи реальные topic_id для трёх агентов
# и aios_root/group_chat_id в [global]
```
> `bridge/agents.toml` — твой личный конфиг (gitignored): `git pull` при обновлении его НЕ трогает, поэтому кастомные агенты и правки не теряются. В репозитории — только `agents.toml.example`.

Подставь путь установки в оба файла permissions (портативно, без `sed -i`):
```bash
for f in bridge/settings.json bridge/settings-sysadmin.json; do
  sed "s|__AIOS_ROOT__|$PWD|g" "$f" > "$f.tmp" && mv "$f.tmp" "$f"
done
```

Материализуй файлы агентов из шаблонов (они gitignored, чтобы обновление их не трогало — в репо лежат только шаблоны):
```bash
# Инструкции рабочих агентов (Сисадмин везёт свой CLAUDE.md в репозитории).
cp agents/assistant/CLAUDE.md.example agents/assistant/CLAUDE.md
cp agents/scriber/CLAUDE.md.example   agents/scriber/CLAUDE.md
# Память каждого агента — из шаблона _template.
for a in assistant sysadmin scriber; do
  cp agents/_template/context.md "agents/$a/context.md"
  cp agents/_template/journal.md "agents/$a/journal.md"
done
```
> Без этого шага у Ассистента и Скрайбера не будет `CLAUDE.md` — Claude CLI не загрузит их роль, и агенты будут «пустыми». Установщик `install.sh` делает это автоматически.

Создай рабочие папки:
```bash
mkdir -p logs/bridge bridge/queue \
         workspace/temp/{inbox,assistant,sysadmin,scriber} workspace/permanent
```

## 5. Авторизация LLM

```bash
claude auth      # или: codex auth / gemini auth
```
Для DeepSeek — просто положи `DEEPSEEK_API_KEY` в `.env`.

## 6. Проверка и запуск

```bash
AIOS_ROOT="$PWD" .venv/bin/python bridge/doctor.py   # должно быть 0 errors
.venv/bin/python bridge/tg_bridge.py                 # запуск
```

Напиши боту в топик 💬 — должен ответить.

## 7. Автозапуск (чтобы работал всегда)

```bash
bash scripts/enable.sh
```
Linux → systemd (`aios-bridge.service`), Mac → launchd. Заодно применяет единое расписание (`scripts/crontab` через `cron-sync.sh`): watchdog, doctor, ежедневная проверка обновлений.

## VPS под root

LLM-CLI не работают под root. Заведи пользователя и поставь всё от него:
```bash
useradd -m -s /bin/bash aios
cp -R ~/aios-public /home/aios/aios-public
chown -R aios:aios /home/aios/aios-public
su - aios -c "cd ~/aios-public && claude auth"
```
`scripts/enable.sh` сам пропишет `User=aios` в systemd-юнит, если запущен под root и пользователь `aios` существует.

## Проблемы

См. `knowledge/system/troubleshooting.md`.
