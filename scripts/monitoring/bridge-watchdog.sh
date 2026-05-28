#!/bin/bash
# bridge-watchdog.sh — следит за мостом aios-public.
#
# Проверяет:
#   1. Процесс tg_bridge.py (без него система мертва) → авторестарт
#   2. Зависание очереди (файлы в queue/ старше N мин) → рестарт
#   3. Свободное место на диске
#
# Алерты идут в группу через BOT_TOKEN (в топик Сисадмина, если задан
# WATCHDOG_TOPIC_ID). Дедуп: критичный алерт шлётся один раз (маркер-файл).
#
# Cron:  */5 * * * *  /path/to/aios-public/scripts/monitoring/bridge-watchdog.sh
# Лог:   {AIOS_ROOT}/logs/bridge/watchdog.log

set -u

# AIOS_ROOT: из env, иначе вычисляем от расположения скрипта (../../)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AIOS_ROOT="${AIOS_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"

PROC=tg_bridge
SERVICE=aios-bridge
LOG=$AIOS_ROOT/logs/bridge/watchdog.log
STATE_DIR=$AIOS_ROOT/logs/bridge/watchdog-state
ENV_FILE=$AIOS_ROOT/bridge/.env

if [[ -f "$ENV_FILE" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a
fi

BOT_TOKEN="${BOT_TOKEN:-}"
TG_BASE="${TG_API_BASE:-https://api.telegram.org}"
CHAT="${AIOS_GROUP_CHAT_ID:-}"
THREAD="${WATCHDOG_TOPIC_ID:-}"   # опционально: topic_id Сисадмина

# Пороги
DISK_FREE_MIN_GB="${WATCHDOG_DISK_MIN_GB:-2}"
QUEUE_STALE_MIN="${WATCHDOG_QUEUE_STALE_MIN:-10}"

mkdir -p "$(dirname "$LOG")" "$STATE_DIR"

ts()  { date '+%Y-%m-%d %H:%M:%S'; }
log() { echo "[$(ts)] $1" >> "$LOG"; }

has_systemd() { command -v systemctl >/dev/null 2>&1 && [[ "$(uname)" != "Darwin" ]]; }

restart_bridge() {
    if has_systemd; then
        systemctl restart "$SERVICE" 2>/dev/null || true
    else
        # Mac / без systemd: запускаем заново через nohup
        ( cd "$AIOS_ROOT" && nohup python3 bridge/tg_bridge.py >> "$AIOS_ROOT/logs/bridge/nohup.log" 2>&1 & )
    fi
}

alert() {
    [[ -z "$BOT_TOKEN" || -z "$CHAT" ]] && { log "alert skipped: BOT_TOKEN/CHAT not set"; return 0; }
    local args=(-d "chat_id=$CHAT" --data-urlencode "text=$1" -d "disable_web_page_preview=true")
    [[ -n "$THREAD" ]] && args+=(-d "message_thread_id=$THREAD")
    curl -s -X POST "$TG_BASE/bot$BOT_TOKEN/sendMessage" "${args[@]}" >/dev/null 2>&1 || true
}
alert_once() {
    local marker=$STATE_DIR/$1
    [[ -f $marker ]] && return 0
    alert "$2"; touch "$marker"
}
clear_alert() { rm -f "$STATE_DIR/$1"; }

# ═════ 1. процесс ═════
check_bridge() {
    if pgrep -f "${PROC}\.py" >/dev/null 2>&1; then clear_alert bridge_down; return 0; fi
    log "Bridge DOWN — restarting"
    alert "⚠️ Мост упал — перезапускаю"
    restart_bridge
    sleep 15
    if pgrep -f "${PROC}\.py" >/dev/null 2>&1; then
        log "restart OK"; alert "✅ Мост перезапущен"; clear_alert bridge_down
    else
        log "restart FAILED"; alert_once bridge_down "❌ Мост не перезапускается — нужен ручной вход на сервер"
    fi
}

# ═════ 2. зависание очереди ═════
check_queue() {
    local q=$AIOS_ROOT/bridge/queue
    [[ -d $q ]] || return 0
    local stale; stale=$(find "$q" -type f -name '*.json' -mmin +"$QUEUE_STALE_MIN" 2>/dev/null | wc -l)
    if (( stale == 0 )); then clear_alert bridge_hung; return 0; fi
    log "Bridge HUNG: $stale stale queue file(s) — restarting"
    alert "⚠️ Мост завис ($stale файл(ов) в очереди >${QUEUE_STALE_MIN} мин). Перезапускаю."
    restart_bridge; sleep 15
    local still; still=$(find "$q" -type f -name '*.json' -mmin +"$QUEUE_STALE_MIN" 2>/dev/null | wc -l)
    if (( still == 0 )); then log "liveness restored"; clear_alert bridge_hung
    else alert_once bridge_hung "❌ Очередь не пошла и после рестарта — нужен ручной разбор"; fi
}

# ═════ 3. диск ═════
check_disk() {
    local free_gb; free_gb=$(df -Pg / 2>/dev/null | awk 'NR==2 {print $4}')
    [[ -z $free_gb ]] && free_gb=$(df -BG / 2>/dev/null | awk 'NR==2 {gsub("G","");print $4}')
    [[ -z $free_gb ]] && return 0
    if (( free_gb < DISK_FREE_MIN_GB )); then
        log "Disk low: ${free_gb}GB"
        alert_once disk_low "🚨 Диск почти полный: свободно ${free_gb}GB (порог ${DISK_FREE_MIN_GB}GB). Почисти workspace/temp/."
    else clear_alert disk_low; fi
}

check_bridge
check_queue
check_disk
exit 0
