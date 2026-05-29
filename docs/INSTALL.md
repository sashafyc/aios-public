# Установка через AI-агента (неинтерактивный режим)

Два способа поставить aios-public:

- **Сам, в терминале** → интерактивный `install.sh` (см. [README](../README.md)) — он проведёт за руку.
- **Через AI-агента (Claude Code / Codex)** → этот документ. Агент выступает «UI-окном»: спрашивает у тебя нужные значения, прогоняет установщик в неинтерактивном режиме и всё делает за тебя.

> Ручной пошаговой установки больше нет — установщик `install.sh` единственный источник правды. Хочешь понять что происходит — открой `install.sh`, он читаемый и комментированный.

---

## Для AI-агента: как ставить

Ты ставишь aios-public за пользователя. Установщик `install.sh` поддерживает
**неинтерактивный режим** — все ответы передаются через переменные окружения,
а не вводятся в терминал.

### Шаг 1. Спроси у пользователя то, что можешь получить только ты-человек

Создать Telegram-бота и группу с топиками может **только человек** — попроси пользователя:

1. **Бот:** @BotFather → `/newbot` → дать тебе **токен**. Затем Bot Settings → Allow Groups **ON**, Group Privacy **OFF**; добавить бота в группу **админом**.
2. **Группа:** создать супергруппу, включить Topics, сделать 3 топика (💬 Ассистент, ⚙️ Система, 🎙 Скрайбер).
3. **id-шники:** переслать сообщение из группы боту @raw_data_bot → дать тебе `chat_id` группы (`-100…`) и три `message_thread_id` (по топику).
4. **Движок:** какой LLM основной — `claude` / `codex` / `gemini` / `deepseek`. Для DeepSeek нужен API-ключ; для claude/codex/gemini — авторизация CLI (см. шаг 2).

### Шаг 2. Убедись, что готово окружение (это можешь сделать сам)

- `python3` **3.11+**, `git`, по возможности `ffmpeg` (Скрайбер). На Linux — доступный `sudo` (для установки пакетов).
- **LLM CLI выбранного движка установлен и авторизован:**
  - claude: `npm i -g @anthropic-ai/claude-code` → `claude auth`
  - codex: `npm i -g @openai/codex` → `codex auth`
  - gemini: `npm i -g @google/gemini-cli` → `gemini auth`
  - deepseek: CLI не нужен, только API-ключ.
  - Авторизация CLI интерактивная (браузер/логин) — если не сделана, попроси пользователя выполнить `<cli> auth`.

### Шаг 3. Запусти установщик неинтерактивно

```bash
AIOS_NONINTERACTIVE=1 \
BOT_TOKEN="123456:AA..." \
AIOS_GROUP_CHAT_ID="-1001234567890" \
TOPIC_ASSISTANT="2" TOPIC_SYSADMIN="3" TOPIC_SCRIBER="4" \
AIOS_RUNNER="claude" \
bash install.sh
```

Переменные:

| Переменная | Обяз. | Что это |
|---|---|---|
| `AIOS_NONINTERACTIVE=1` | да | включает неинтерактивный режим (или флаг `--non-interactive`) |
| `BOT_TOKEN` | да | токен бота от @BotFather |
| `AIOS_GROUP_CHAT_ID` | да | id группы, формат `-100…` |
| `TOPIC_ASSISTANT` / `TOPIC_SYSADMIN` / `TOPIC_SCRIBER` | да | `message_thread_id` топиков (числа) |
| `AIOS_RUNNER` | нет | `claude` (по умолч.) / `codex` / `gemini` / `deepseek` |
| `DEEPSEEK_API_KEY` | если `AIOS_RUNNER=deepseek` | ключ DeepSeek API |
| `ASSEMBLYAI_API_KEY` | нет | включает голосовые (транскрибация) |
| `AIOS_INSTALL_DIR` | нет | куда ставить (по умолч. `~/aios-public`) |
| `AIOS_INSTALL_PKGS` | нет | `1` — разрешить Сисадмину ставить системные пакеты (sudoers) |

В неинтерактивном режиме установщик: проверяет окружение, копирует код, ставит зависимости,
генерирует `.env` / `agents.toml` / `settings*.json`, материализует файлы агентов — без вопросов.
Если обязательная переменная не задана или невалидна — выходит с понятной ошибкой (не зависает).
Вся «UI-текстовка» идёт в stdout — читай её и при необходимости релей пользователю.

### Шаг 4. Автозапуск и проверка

```bash
bash "${AIOS_INSTALL_DIR:-$HOME/aios-public}/scripts/enable.sh"   # systemd/launchd + единое расписание
AIOS_ROOT="${AIOS_INSTALL_DIR:-$HOME/aios-public}" \
  "${AIOS_INSTALL_DIR:-$HOME/aios-public}/.venv/bin/python" \
  "${AIOS_INSTALL_DIR:-$HOME/aios-public}/bridge/doctor.py"        # должно быть 0 errors
```

Доложи пользователю итог doctor. Если `runner:<движок>` показывает «не подключён» —
сообщи, что нужно авторизовать CLI или дать API-ключ.

---

## VPS под root

LLM-CLI не работают под root. Установщик сам заводит пользователя `aios` и ставит всё от него
(`INSTALL_DIR=/home/aios/aios-public`), а `scripts/enable.sh` прописывает `User=aios` в systemd.
Авторизацию CLI выполняй под ним: `su - aios -c "claude auth"`.

## Проблемы

См. `knowledge/system/troubleshooting.md`.
