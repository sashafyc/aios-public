# Установка вручную

Если `install.sh` не подошёл (или хочешь понимать что происходит) — поставь руками. Это те же шаги, что делает установщик.

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

В `bridge/agents.toml` пропиши реальные `topic_id` для трёх агентов и `aios_root`/`group_chat_id` в `[global]`.

Подставь путь в `settings.json`:
```bash
sed -i "s|__AIOS_ROOT__|$PWD|g" bridge/settings.json
```

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
Linux → systemd (`aios-bridge.service`), Mac → launchd. Заодно ставит watchdog в cron.

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
