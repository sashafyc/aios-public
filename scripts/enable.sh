#!/usr/bin/env bash
# enable.sh — настройка автозапуска моста.
#   Linux → systemd unit (aios-bridge.service)
#   macOS → launchd plist (com.aios-public.bridge)
# Запуск:  bash scripts/enable.sh
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AIOS_ROOT="${AIOS_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
PY="$AIOS_ROOT/.venv/bin/python"
RUN_USER="$(id -un)"

b()  { printf '\033[1m%s\033[0m\n' "$*"; }
ok() { printf '   \033[32m✅ %s\033[0m\n' "$*"; }

[[ -x "$PY" ]] || { echo "❌ venv не найден ($PY). Сначала запусти install.sh"; exit 1; }

# ───────── Linux: systemd ─────────
enable_systemd() {
    b "Настраиваю systemd (aios-bridge.service)"
    # на VPS под root мост должен работать от пользователя aios
    local svc_user="$RUN_USER"
    if [[ "$(id -u)" == "0" ]] && id aios >/dev/null 2>&1; then svc_user="aios"; fi

    local unit=/etc/systemd/system/aios-bridge.service
    local sudo=""; [[ "$(id -u)" != "0" ]] && sudo="sudo"

    $sudo tee "$unit" >/dev/null <<EOF
[Unit]
Description=aios-public Telegram bridge
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$svc_user
WorkingDirectory=$AIOS_ROOT
EnvironmentFile=$AIOS_ROOT/bridge/.env
ExecStart=$PY $AIOS_ROOT/bridge/tg_bridge.py
Restart=always
RestartSec=10
StandardOutput=append:$AIOS_ROOT/logs/bridge/service.log
StandardError=append:$AIOS_ROOT/logs/bridge/service.log

[Install]
WantedBy=multi-user.target
EOF
    $sudo systemctl daemon-reload
    $sudo systemctl enable --now aios-bridge.service
    ok "systemd: aios-bridge запущен и добавлен в автозапуск"

    # Узкое sudoers-правило: сервисный юзер может рестартить ТОЛЬКО aios-bridge
    # без пароля. Нужно чтобы Сисадмин мог перезапускать мост после правки .env.
    local sysctl_bin; sysctl_bin="$(command -v systemctl || echo /usr/bin/systemctl)"
    local sudoers=/etc/sudoers.d/aios-bridge
    if [[ "$(id -u)" == "0" || -n "$sudo" ]]; then
        echo "$svc_user ALL=(root) NOPASSWD: $sysctl_bin restart aios-bridge, $sysctl_bin restart aios-bridge.service" \
            | $sudo tee "$sudoers" >/dev/null
        $sudo chmod 440 "$sudoers"
        # валидация синтаксиса sudoers (откат если сломали)
        if $sudo visudo -cf "$sudoers" >/dev/null 2>&1; then
            ok "sudoers: $svc_user может рестартить мост (sudo systemctl restart aios-bridge)"
        else
            $sudo rm -f "$sudoers"; warn "sudoers-правило не прошло валидацию — пропущено"
        fi
    fi
    echo "   Статус:  systemctl status aios-bridge"
    echo "   Логи:    journalctl -u aios-bridge -f"
}

# ───────── macOS: launchd ─────────
enable_launchd() {
    b "Настраиваю launchd (автозапуск на Mac)"
    local label="com.aios-public.bridge"
    local plist="$HOME/Library/LaunchAgents/$label.plist"
    mkdir -p "$HOME/Library/LaunchAgents"

    cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string>
    <string>$AIOS_ROOT/bridge/tg_bridge.py</string>
  </array>
  <key>WorkingDirectory</key><string>$AIOS_ROOT</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$AIOS_ROOT/logs/bridge/service.log</string>
  <key>StandardErrorPath</key><string>$AIOS_ROOT/logs/bridge/service.log</string>
</dict>
</plist>
EOF
    launchctl unload "$plist" 2>/dev/null || true
    launchctl load "$plist"
    ok "launchd: $label загружен (автозапуск при входе)"
    echo "   Остановить:  launchctl unload $plist"
    echo "   Логи:        tail -f $AIOS_ROOT/logs/bridge/service.log"
}

# ───────── cron: watchdog ─────────
enable_watchdog() {
    b "Добавляю watchdog в cron (каждые 5 минут)"
    local line="*/5 * * * * AIOS_ROOT=$AIOS_ROOT bash $AIOS_ROOT/scripts/monitoring/bridge-watchdog.sh"
    if crontab -l 2>/dev/null | grep -qF "bridge-watchdog.sh"; then
        ok "watchdog уже в cron"
    else
        ( crontab -l 2>/dev/null; echo "$line" ) | crontab -
        ok "watchdog добавлен в cron"
    fi
}

mkdir -p "$AIOS_ROOT/logs/bridge"
case "$(uname -s)" in
    Linux)  enable_systemd ;;
    Darwin) enable_launchd ;;
    *) echo "❌ Неподдерживаемая ОС для автозапуска"; exit 1 ;;
esac
enable_watchdog 2>/dev/null || echo "   (cron недоступен — watchdog пропущен)"
echo ""
echo "Готово. Мост работает в фоне."
