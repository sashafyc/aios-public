#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════
#  aios-public — интерактивный установщик
#  Способ 1:  curl -fsSL <url>/install.sh | bash
#  Способ 2:  ./install.sh   (из распакованного репозитория)
#
#  Ведёт за руку: создаёшь группу и бота в Telegram, выбираешь LLM,
#  опционально подключаешь голосовые. Конфиги генерятся автоматически.
# ════════════════════════════════════════════════════════════
set -uo pipefail

REPO_URL="${AIOS_REPO_URL:-https://github.com/sashafyc/aios-public}"
REPO_BRANCH="${AIOS_REPO_BRANCH:-main}"

# ───────── вывод ─────────
b()    { printf '\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '   \033[32m✅ %s\033[0m\n' "$*"; }
warn() { printf '   \033[33m⚠️  %s\033[0m\n' "$*"; }
err()  { printf '   \033[31m❌ %s\033[0m\n' "$*"; }
hr()   { printf ' ───────────────────────────────\n'; }

# read из /dev/tty — работает даже когда скрипт пришёл через `curl | bash`
ask() {  # ask "Вопрос: " VARNAME [default]
    local prompt="$1" __var="$2" def="${3:-}" reply
    if [[ -n "$def" ]]; then prompt="${prompt}[$def] "; fi
    printf '%s' "$prompt" > /dev/tty
    IFS= read -r reply < /dev/tty || reply=""
    [[ -z "$reply" && -n "$def" ]] && reply="$def"
    printf -v "$__var" '%s' "$reply"
}
confirm() {  # confirm "Готово?" → 0 если y/yes/да
    local reply; ask "$1 (y/n): " reply
    [[ "$reply" =~ ^([yYдД]|yes|да)$ ]]
}

banner() {
cat > /dev/tty <<'EOF'

 ╔══════════════════════════════════════════╗
 ║  aios-public — AI-команда в Telegram      ║
 ║  Установка ~5 минут. Проведу за руку.     ║
 ╚══════════════════════════════════════════╝
EOF
}

# ───────── определение ОС/менеджера пакетов ─────────
OS=""; PKG=""; IS_ROOT=0
detect_os() {
    case "$(uname -s)" in
        Darwin) OS="mac" ;;
        Linux)
            OS="linux"
            grep -qiE "microsoft|wsl" /proc/version 2>/dev/null && OS="wsl"
            ;;
        *) err "Неподдерживаемая ОС: $(uname -s). Нужен macOS, Linux или WSL2."; exit 1 ;;
    esac
    if   command -v apt-get >/dev/null 2>&1; then PKG="apt"
    elif command -v brew    >/dev/null 2>&1; then PKG="brew"
    elif command -v dnf     >/dev/null 2>&1; then PKG="dnf"
    fi
    [[ "$(id -u)" == "0" ]] && IS_ROOT=1
}

pkg_install() {  # pkg_install <пакеты...>
    case "$PKG" in
        apt)  sudo apt-get update -qq && sudo apt-get install -y -qq "$@" ;;
        brew) brew install "$@" ;;
        dnf)  sudo dnf install -y -q "$@" ;;
        *)    return 1 ;;
    esac
}

# ───────── Шаг 0: система и зависимости ─────────
PYTHON=""
step_system() {
    b " Шаг 0: Проверяю систему"; hr
    ok "ОС: $OS (менеджер пакетов: ${PKG:-нет})"

    # Homebrew на Mac
    if [[ "$OS" == "mac" && -z "$PKG" ]]; then
        warn "Homebrew не найден — нужен для установки зависимостей"
        if confirm " Установить Homebrew?"; then
            /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" < /dev/tty
            PKG="brew"
        else
            err "Без Homebrew не смогу поставить python/ffmpeg. Прерываю."; exit 1
        fi
    fi

    # Python 3.11+ (нужен tomllib)
    for c in python3.13 python3.12 python3.11 python3; do
        if command -v "$c" >/dev/null 2>&1; then
            local v; v=$("$c" -c 'import sys;print(sys.version_info[0]*100+sys.version_info[1])' 2>/dev/null || echo 0)
            if (( v >= 311 )); then PYTHON="$c"; break; fi
        fi
    done
    if [[ -z "$PYTHON" ]]; then
        warn "Python 3.11+ не найден — устанавливаю"
        if [[ "$PKG" == "apt" ]]; then pkg_install python3 python3-venv python3-pip
        elif [[ "$PKG" == "brew" ]]; then pkg_install python@3.12
        else pkg_install python3; fi
        PYTHON="python3"
    fi
    ok "Python: $($PYTHON --version 2>&1 | awk '{print $2}') ($PYTHON)"

    # git (нужен для remote-режима)
    command -v git >/dev/null 2>&1 || pkg_install git || true

    # ffmpeg + yt-dlp (для Скрайбера; не критично если не встанут)
    command -v ffmpeg >/dev/null 2>&1 && ok "ffmpeg: есть" || {
        warn "ffmpeg не найден — ставлю (нужен для транскрибации)"
        pkg_install ffmpeg && ok "ffmpeg установлен" || warn "ffmpeg не установлен (Скрайбер с видео может не работать)"
    }
}

# ───────── VPS под root: создаём пользователя aios ─────────
RUN_USER=""
setup_user() {
    if [[ "$OS" != "mac" && "$IS_ROOT" == "1" ]]; then
        b " Обнаружен запуск под root на сервере"; hr
        warn "LLM-CLI (Claude/Codex) запрещают работу под root."
        printf "    Создаю отдельного пользователя 'aios' — это безопасно и правильно.\n" > /dev/tty
        if ! id aios >/dev/null 2>&1; then
            useradd -m -s /bin/bash aios
            ok "Пользователь aios создан"
        else
            ok "Пользователь aios уже есть"
        fi
        RUN_USER="aios"
        INSTALL_DIR="/home/aios/aios-public"
    else
        RUN_USER="$(id -un)"
        INSTALL_DIR="${AIOS_INSTALL_DIR:-$HOME/aios-public}"
    fi
}

# ───────── получение кода (local | remote) ─────────
SRC_DIR=""
fetch_code() {
    local here; here="$(cd "$(dirname "${BASH_SOURCE[0]:-/dev/null}")" 2>/dev/null && pwd || echo "")"
    if [[ -n "$here" && -f "$here/bridge/tg_bridge.py" ]]; then
        # local mode — запущен из репозитория
        SRC_DIR="$here"
        b " Режим: локальная установка из $SRC_DIR"; hr
    else
        # remote mode — клонируем
        b " Режим: загрузка из $REPO_URL"; hr
        command -v git >/dev/null 2>&1 || { err "git не найден, не могу склонировать"; exit 1; }
        local tmp; tmp="$(mktemp -d)"
        git clone --depth 1 -b "$REPO_BRANCH" "$REPO_URL" "$tmp/aios-public" >/dev/null 2>&1 \
            && ok "Код загружен" || { err "Не удалось склонировать $REPO_URL"; exit 1; }
        SRC_DIR="$tmp/aios-public"
    fi

    # Копируем в INSTALL_DIR (если это не та же папка)
    if [[ "$SRC_DIR" != "$INSTALL_DIR" ]]; then
        mkdir -p "$INSTALL_DIR"
        cp -R "$SRC_DIR"/. "$INSTALL_DIR"/
        ok "Установлено в $INSTALL_DIR"
    else
        ok "Работаю прямо в $INSTALL_DIR"
    fi
}

# ───────── venv + зависимости ─────────
setup_venv() {
    b " Ставлю Python-зависимости"; hr
    "$PYTHON" -m venv "$INSTALL_DIR/.venv"
    local pip="$INSTALL_DIR/.venv/bin/pip"
    "$pip" install -q --upgrade pip
    "$pip" install -q -r "$INSTALL_DIR/bridge/requirements.txt"
    ok "Зависимости моста установлены (aiogram, aiohttp, httpx)"
    # Библиотеки навыков Ассистента (документы + анализ)
    "$pip" install -q -r "$INSTALL_DIR/bridge/requirements-skills.txt" 2>/dev/null \
        && ok "Библиотеки навыков установлены (openpyxl, python-pptx, reportlab, pypdf, pdfplumber, pandas)" \
        || warn "Часть библиотек навыков не встала — Ассистент доставит при необходимости"
    # yt-dlp в тот же venv (для Скрайбера)
    "$pip" install -q yt-dlp 2>/dev/null && ok "yt-dlp установлен (YouTube в Скрайбере)" \
        || warn "yt-dlp не установлен (YouTube в Скрайбере не будет)"
}

# ───────── Шаг 1: Telegram-группа ─────────
GROUP_CHAT_ID=""; TOPIC_ASSISTANT=""; TOPIC_SYSADMIN=""; TOPIC_SCRIBER=""
step_group() {
    b " Шаг 1: Telegram-группа"; hr
    cat > /dev/tty <<'EOF'
    Создадим группу для агентов. По шагам:
    1. Telegram → "Создать группу" (New Group)
    2. Назови как хочешь (напр. "Моя AI-команда")
    3. Добавь любой контакт (потом удалишь)
    4. Открой настройки группы → Group Type → Supergroup
    5. Включи Topics (Темы)
    6. Создай 3 топика:
       • 💬 Ассистент   • ⚙️ Система   • 🎙 Скрайбер
EOF
    until confirm " Группа с 3 топиками готова?"; do :; done

    cat > /dev/tty <<'EOF'

    Теперь нужен ID группы:
    → Добавь в группу бота @raw_data_bot (или @getidsbot)
    → Он пришлёт JSON, найди "chat_id": -100XXXXXXXXXX
EOF
    ask " ID группы (-100...): " GROUP_CHAT_ID

    cat > /dev/tty <<'EOF'

    И topic_id каждого топика:
    → Напиши сообщение в топик, перешли его @raw_data_bot
    → В ответе найди "message_thread_id"
EOF
    ask " topic_id для 💬 Ассистент: " TOPIC_ASSISTANT
    ask " topic_id для ⚙️ Система: "   TOPIC_SYSADMIN
    ask " topic_id для 🎙 Скрайбер: "  TOPIC_SCRIBER
    ok "Группа: $GROUP_CHAT_ID (топики: $TOPIC_ASSISTANT/$TOPIC_SYSADMIN/$TOPIC_SCRIBER)"
}

# ───────── Шаг 2: бот ─────────
BOT_TOKEN=""
step_bot() {
    b " Шаг 2: Telegram-бот"; hr
    cat > /dev/tty <<'EOF'
    1. Открой @BotFather → /newbot
    2. Придумай имя и username (...bot)
    3. BotFather пришлёт токен (длинная строка с двоеточием)
EOF
    ask " Токен бота: " BOT_TOKEN
    cat > /dev/tty <<'EOF'

    Настрой бота у @BotFather:
    4. /mybots → выбери бота → Bot Settings
    5. Allow Groups → ON
    6. Group Privacy → OFF   (важно — иначе бот не видит сообщения!)
    7. Добавь бота в группу → сделай Админом (статус, галочки не нужны)
EOF
    until confirm " Бот настроен, добавлен в группу и админ?"; do :; done
    ok "Бот подключён"
}

# ───────── Шаг 3: выбор LLM ─────────
RUNNER="claude"; RUNNER_MODEL="claude-sonnet-4-6"; RUNNER_STREAM="true"; DEEPSEEK_API_KEY=""
step_llm() {
    b " Шаг 3: На каком LLM работают агенты?"; hr
    cat > /dev/tty <<'EOF'
    1) Claude       — лучший, полноценный tool use (рекомендуем)
    2) Codex/ChatGPT— нужна подписка ChatGPT Plus (~$20/мес)
    3) DeepSeek     — самый дешёвый (API key)
    4) Gemini       — бесплатно (лимит/день)
EOF
    local choice; ask " Выбор (1-4): " choice "1"
    case "$choice" in
        2) RUNNER="codex";    RUNNER_MODEL="gpt-5.4-codex";   RUNNER_STREAM="false" ;;
        3) RUNNER="deepseek"; RUNNER_MODEL="deepseek-v4-pro"; RUNNER_STREAM="false" ;;
        4) RUNNER="gemini";   RUNNER_MODEL="gemini";          RUNNER_STREAM="false" ;;
        *) RUNNER="claude";   RUNNER_MODEL="claude-sonnet-4-6"; RUNNER_STREAM="true" ;;
    esac
    ok "Выбран runner: $RUNNER ($RUNNER_MODEL)"

    case "$RUNNER" in
        claude) _ensure_cli claude "npm i -g @anthropic-ai/claude-code" "claude" ;;
        codex)  _ensure_cli codex  "npm i -g @openai/codex"            "codex"  ;;
        gemini) _ensure_cli gemini "npm i -g @google/gemini-cli"       "gemini" ;;
        deepseek)
            ask " DeepSeek API key: " DEEPSEEK_API_KEY
            ok "Ключ DeepSeek сохраню в .env"
            ;;
    esac
}

# проверка/установка CLI + подсказка про авторизацию (под нужным юзером)
_ensure_cli() {  # _ensure_cli <cmd> <install-hint> <auth-cmd>
    local cmd="$1" hint="$2" authcmd="$3"
    if command -v "$cmd" >/dev/null 2>&1; then
        ok "$cmd CLI найден"
    else
        warn "$cmd CLI не найден"
        command -v node >/dev/null 2>&1 || { pkg_install nodejs npm 2>/dev/null || pkg_install node 2>/dev/null || true; }
        printf "    Установи вручную:  %s\n" "$hint" > /dev/tty
        until confirm " Установил $cmd?"; do :; done
    fi
    # авторизация
    if [[ -n "$RUN_USER" && "$RUN_USER" != "$(id -un)" ]]; then
        printf "    Авторизуйся под пользователем %s:\n      su - %s -c '%s auth'\n" "$RUN_USER" "$RUN_USER" "$authcmd" > /dev/tty
    else
        printf "    Авторизуйся:  %s auth   (или login)\n" "$authcmd" > /dev/tty
    fi
    until confirm " Авторизация $cmd выполнена?"; do :; done
}

# ───────── Шаг 4: голосовые (опц.) ─────────
ASSEMBLYAI_API_KEY=""
step_voice() {
    b " Шаг 4: Голосовые и транскрибация (опционально)"; hr
    printf "    Нужен AssemblyAI (бесплатно \$50 на старте: assemblyai.com).\n" > /dev/tty
    if confirm " Подключить голосовые сейчас?"; then
        ask " AssemblyAI API key: " ASSEMBLYAI_API_KEY
        ok "Ключ AssemblyAI сохраню"
    else
        warn "Пропускаю — подключишь позже через Сисадмина"
    fi
}

# ───────── Шаг 4.5: автономность Сисадмина (системные пакеты) ─────────
ALLOW_PKG="no"
step_pkg_perm() {
    # На Mac brew работает без root — вопрос не нужен.
    [[ "$OS" == "mac" ]] && return 0
    b " Шаг 4.5: Автономность Сисадмина (опционально)"; hr
    cat > /dev/tty <<'EOF'
    Иногда агентам нужен системный пакет (ffmpeg, OCR, конвертеры).
    Их установка требует прав root.

    • Разрешить — Сисадмин ставит такие пакеты сам (apt-get install),
      и тебе не нужно лезть на сервер. Удобно.
    • Не разрешать — безопаснее: при необходимости Сисадмин даст тебе
      команду, выполнишь сам. (Python-библиотеки он ставит сам в любом случае.)
EOF
    if confirm " Разрешить Сисадмину ставить системные пакеты сам?"; then
        ALLOW_PKG="yes"; ok "Разрешено — настрою права при запуске"
    else
        ALLOW_PKG="no"; ok "Безопасный режим — без root у агента"
    fi
}

_write_pkg_sudoers() {
    [[ "$ALLOW_PKG" == "yes" && "$OS" != "mac" ]] || return 0
    local sudo=""; [[ "$(id -u)" != "0" ]] && sudo="sudo"
    local pm_path rule
    if   command -v apt-get >/dev/null 2>&1; then pm_path="$(command -v apt-get)"; rule="$pm_path update, $pm_path install -y *, $pm_path install *"
    elif command -v dnf     >/dev/null 2>&1; then pm_path="$(command -v dnf)";     rule="$pm_path install -y *, $pm_path install *"
    else return 0; fi
    local sudoers=/etc/sudoers.d/aios-pkg
    echo "$RUN_USER ALL=(root) NOPASSWD: $rule" | $sudo tee "$sudoers" >/dev/null 2>&1 || return 0
    $sudo chmod 440 "$sudoers" 2>/dev/null
    if $sudo visudo -cf "$sudoers" >/dev/null 2>&1; then
        ok "Сисадмину разрешена установка системных пакетов (sudo apt/dnf install)"
    else
        $sudo rm -f "$sudoers"; warn "sudoers для пакетов не прошёл валидацию — пропущено"
    fi
}

# ───────── Шаг 5: генерация конфигов + запуск ─────────
step_generate() {
    b " Шаг 5: Генерирую конфиги"; hr
    local bridge="$INSTALL_DIR/bridge"

    # .env
    cat > "$bridge/.env" <<EOF
BOT_TOKEN=$BOT_TOKEN
AIOS_GROUP_CHAT_ID=$GROUP_CHAT_ID
AIOS_ROOT=$INSTALL_DIR
DEEPSEEK_API_KEY=$DEEPSEEK_API_KEY
ASSEMBLYAI_API_KEY=$ASSEMBLYAI_API_KEY
TIMEZONE_OFFSET_HOURS=3
DAILY_RESET_HOUR=6
DAILY_RESET_MINUTE=30
WATCHDOG_TOPIC_ID=$TOPIC_SYSADMIN
EOF
    chmod 600 "$bridge/.env"
    ok ".env создан (chmod 600)"

    # agents.toml — подставляем реальные значения
    _gen_agents_toml "$bridge/agents.toml"
    ok "agents.toml создан (3 агента)"

    # settings*.json — подставляем путь установки в плейсхолдеры
    for sf in settings.json settings-sysadmin.json; do
        [[ -f "$bridge/$sf" ]] || continue
        sed "s|__AIOS_ROOT__|$INSTALL_DIR|g" "$bridge/$sf" > "$bridge/$sf.tmp" && mv "$bridge/$sf.tmp" "$bridge/$sf"
    done
    ok "settings.json + settings-sysadmin.json (permissions) настроены"

    # директории
    mkdir -p "$INSTALL_DIR/logs/bridge" "$bridge/queue" \
             "$INSTALL_DIR/workspace/temp/inbox" \
             "$INSTALL_DIR/workspace/temp/assistant" \
             "$INSTALL_DIR/workspace/temp/sysadmin" \
             "$INSTALL_DIR/workspace/temp/scriber" \
             "$INSTALL_DIR/workspace/permanent"
    ok "Рабочие директории созданы"

    # симлинки агентов (на случай если cp их развернул)
    for a in assistant sysadmin scriber _template; do
        local d="$INSTALL_DIR/agents/$a"
        [[ -d "$d" ]] || continue
        ln -sf CLAUDE.md "$d/AGENTS.md"
        ln -sf CLAUDE.md "$d/GEMINI.md"
    done
    ok "Симлинки AGENTS.md/GEMINI.md обновлены"

    # права для пользователя aios (VPS)
    if [[ -n "$RUN_USER" && "$RUN_USER" != "$(id -un)" ]]; then
        chown -R "$RUN_USER:$RUN_USER" "$INSTALL_DIR"
        ok "Права переданы пользователю $RUN_USER"
    fi

    # опциональное право Сисадмину ставить системные пакеты
    _write_pkg_sudoers
}

_gen_agents_toml() {  # _gen_agents_toml <path>
    cat > "$1" <<EOF
# agents.toml — сгенерирован install.sh. Hot-reload: правки подхватываются за 60с.
[global]
aios_root = "$INSTALL_DIR"
group_chat_id = $GROUP_CHAT_ID

[agents.assistant]
display_name = "Ассистент"
topic_id = $TOPIC_ASSISTANT
bot_token_env = "BOT_TOKEN"
workdir = "agents/assistant"
model = "$RUNNER_MODEL"
runner_type = "$RUNNER"
role = "Главный рабочий агент: ресёрч, код, сайты, презентации, документы. Хаб делегации."
can_delegate_to = ["scriber"]
stream = $RUNNER_STREAM
timeout_s = 7200

[agents.sysadmin]
display_name = "Сисадмин"
topic_id = $TOPIC_SYSADMIN
bot_token_env = "BOT_TOKEN"
workdir = "agents/sysadmin"
model = "$RUNNER_MODEL"
runner_type = "$RUNNER"
role = "Управление системой через диалог: агенты, runner'ы, API, здоровье."
stream = $RUNNER_STREAM
timeout_s = 7200
settings_path = "bridge/settings-sysadmin.json"

[agents.scriber]
display_name = "Скрайбер"
topic_id = $TOPIC_SCRIBER
bot_token_env = "BOT_TOKEN"
workdir = "agents/scriber"
model = "$RUNNER_MODEL"
runner_type = "$RUNNER"
role = "Транскрибация: голосовые, аудио, видео, YouTube."
stream = $RUNNER_STREAM
timeout_s = 7200
EOF
}

# ───────── запуск + проверка ─────────
step_launch() {
    b " Запускаю мост и проверяю"; hr
    local py="$INSTALL_DIR/.venv/bin/python"

    # doctor
    ( cd "$INSTALL_DIR" && AIOS_ROOT="$INSTALL_DIR" "$py" bridge/doctor.py ) > /dev/tty 2>&1 || true

    cat > /dev/tty <<EOF

 ╔══════════════════════════════════════════╗
 ║  ✅ Установка завершена!                  ║
 ╚══════════════════════════════════════════╝

 Папка: $INSTALL_DIR

 Запустить мост сейчас (в фоне):
   cd $INSTALL_DIR && .venv/bin/python bridge/tg_bridge.py

 Автозапуск (чтобы работал всегда):
   bash $INSTALL_DIR/scripts/enable.sh

 Проверь в Telegram:
   💬 Напиши в Ассистент: "привет"
   ⚙️ Напиши в Систему: "статус"
   🎙 Скинь в Скрайбер голосовое
EOF
}

# ───────── main ─────────
main() {
    banner
    detect_os
    step_system
    setup_user
    fetch_code
    setup_venv
    step_group
    step_bot
    step_llm
    step_voice
    step_pkg_perm
    step_generate
    step_launch
}
main "$@"
