#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════
#  update.sh — безопасное обновление aios-public до новой версии
#
#  Что делает:
#    • git fetch → сравнивает локальную версию с remote
#    • показывает changelog (CHANGELOG.md или git log)
#    • бэкапит .env / agents.toml / settings*.json + git stash правок
#    • git pull --ff-only (при конфликте — НЕ ломает, выходит с ошибкой)
#    • заново подставляет __AIOS_ROOT__ в settings*.json
#    • прогоняет тесты; при провале — откат к старому коммиту
#    • рестартит сервис (systemd / launchd)
#
#  Флаги:
#    --check   только проверить наличие обновления (ничего не меняет)
#              exit 0 если обнова есть, exit 0 + сообщение если нет
#    --yes     не задавать вопросов (для Сисадмина / cron)
#
#  Запуск:  bash scripts/update.sh [--check] [--yes]
# ════════════════════════════════════════════════════════════
set -euo pipefail

# ───────── пути ─────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AIOS_ROOT="${AIOS_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
BRIDGE="$AIOS_ROOT/bridge"
PY="$AIOS_ROOT/.venv/bin/python"
BRANCH="${AIOS_REPO_BRANCH:-main}"

# ───────── вывод ─────────
b()    { printf '\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '   \033[32m✅ %s\033[0m\n' "$*"; }
warn() { printf '   \033[33m⚠️  %s\033[0m\n' "$*"; }
err()  { printf '   \033[31m❌ %s\033[0m\n' "$*" >&2; }
hr()   { printf ' ───────────────────────────────\n'; }

# ───────── флаги ─────────
CHECK_ONLY=0
ASSUME_YES=0
for arg in "$@"; do
    case "$arg" in
        --check) CHECK_ONLY=1 ;;
        --yes|-y) ASSUME_YES=1 ;;
        -h|--help)
            grep -E '^#' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) err "Неизвестный флаг: $arg"; exit 2 ;;
    esac
done

confirm() {  # confirm "Вопрос?" → 0 если да; при --yes всегда да
    [[ "$ASSUME_YES" == "1" ]] && return 0
    local reply
    printf '%s (y/n): ' "$1" > /dev/tty
    IFS= read -r reply < /dev/tty || reply=""
    [[ "$reply" =~ ^([yYдД]|yes|да)$ ]]
}

# ───────── предусловия ─────────
cd "$AIOS_ROOT"
command -v git >/dev/null 2>&1 || { err "git не найден"; exit 1; }
[[ -d "$AIOS_ROOT/.git" ]] || { err "$AIOS_ROOT — не git-репозиторий. Обновление через git недоступно (вероятно ставили распаковкой архива)."; exit 1; }

# ───────── версии ─────────
# Человекочитаемая версия: ближайший тег, иначе короткий хеш.
describe_version() {  # describe_version <ref>
    git describe --tags --always "$1" 2>/dev/null || git rev-parse --short "$1"
}

b " Проверяю обновления aios-public"; hr
git fetch --tags --quiet origin "$BRANCH" 2>/dev/null || { err "git fetch не удался — проверь сеть/доступ к origin"; exit 1; }

LOCAL_REF="$(git rev-parse HEAD)"
REMOTE_REF="$(git rev-parse "origin/$BRANCH")"
LOCAL_VER="$(describe_version HEAD)"
REMOTE_VER="$(describe_version "origin/$BRANCH")"

if [[ "$LOCAL_REF" == "$REMOTE_REF" ]]; then
    ok "Уже последняя версия ($LOCAL_VER). Обновляться не нужно."
    exit 0
fi

# Есть обновление.
b " Доступна новая версия:  $LOCAL_VER → $REMOTE_VER"

# ───────── changelog ─────────
show_changelog() {
    hr
    b " Что нового:"
    if [[ -f "$AIOS_ROOT/CHANGELOG.md" ]]; then
        # показываем верх CHANGELOG (свежие записи сверху)
        head -n 40 "$AIOS_ROOT/CHANGELOG.md" | sed 's/^/   /'
    else
        git log --no-merges --pretty=format:'   • %s' "$LOCAL_REF..$REMOTE_REF" | head -n 30
        printf '\n'
    fi
    hr
}
show_changelog

# --check: только сообщить, ничего не делать. Обнова есть → exit 0.
if [[ "$CHECK_ONLY" == "1" ]]; then
    ok "Доступно обновление $REMOTE_VER. Для установки: bash scripts/update.sh --yes"
    exit 0
fi

if ! confirm " Обновиться до $REMOTE_VER? Я сделаю бэкап и при провале откачусь"; then
    warn "Отменено пользователем. Ничего не изменено."
    exit 0
fi

# ───────── бэкап ─────────
TS="$(date +%Y%m%d-%H%M%S)"
BACKUP_DIR="$AIOS_ROOT/.backups/$TS"
mkdir -p "$BACKUP_DIR"
b " Делаю бэкап в .backups/$TS"; hr

# 1) Критичные конфиги (копией — на случай любого сценария).
for f in "$BRIDGE/.env" "$BRIDGE/agents.toml" "$BRIDGE/settings.json" "$BRIDGE/settings-sysadmin.json"; do
    if [[ -f "$f" ]]; then
        cp -p "$f" "$BACKUP_DIR/$(basename "$f")"
        ok "сохранён $(basename "$f")"
    fi
done

# 2) Папка agents/ целиком (кастомные агенты + правки инструкций).
if [[ -d "$AIOS_ROOT/agents" ]]; then
    cp -R "$AIOS_ROOT/agents" "$BACKUP_DIR/agents"
    ok "сохранена папка agents/ (кастомные агенты и правки инструкций)"
fi

# 3) Запоминаем коммит до обновления — точка отката.
echo "$LOCAL_REF" > "$BACKUP_DIR/PREV_COMMIT"

# ───────── stash локальных правок ─────────
# agents.toml / settings*.json / правки CLAUDE.md отслеживаются git и могут
# конфликтовать с pull. Прячем их в stash, чтобы pull прошёл fast-forward.
STASHED=0
if [[ -n "$(git status --porcelain)" ]]; then
    git stash push -u -m "aios-update-$TS" >/dev/null 2>&1 && STASHED=1
    [[ "$STASHED" == "1" ]] && ok "локальные правки спрятаны в git stash (aios-update-$TS)"
fi

# helper: восстановить stash обратно (при откате/ошибке)
restore_stash() {
    if [[ "$STASHED" == "1" ]]; then
        git stash pop >/dev/null 2>&1 || warn "git stash pop с конфликтом — правки в stash: git stash list"
    fi
}

# ───────── git pull (fast-forward) ─────────
b " Обновляю код (git pull --ff-only)"; hr
if ! git merge --ff-only "origin/$BRANCH" >/dev/null 2>&1; then
    err "Не удалось обновиться fast-forward (история разошлась или конфликт)."
    err "Код НЕ изменён. Твои правки в безопасности: git stash list → aios-update-$TS"
    restore_stash
    err "Бэкап конфигов: $BACKUP_DIR"
    exit 1
fi
ok "Код обновлён до $REMOTE_VER"

# ───────── вернуть локальные правки конфигов ─────────
# Пробуем вернуть stash поверх нового кода. Конфликт по отслеживаемым файлам
# (agents.toml / settings*.json / CLAUDE.md) возможен — тогда оставляем бэкап
# и честно предупреждаем, НЕ затирая ничего молча.
CONFLICTS=""
if [[ "$STASHED" == "1" ]]; then
    if git stash pop >/dev/null 2>&1; then
        ok "Локальные правки конфигов возвращены поверх нового кода"
    else
        CONFLICTS="$(git diff --name-only --diff-filter=U 2>/dev/null || true)"
        warn "Конфликт при возврате правок — нужно решить вручную:"
        [[ -n "$CONFLICTS" ]] && printf '%s\n' "$CONFLICTS" | sed 's/^/      /'
        warn "Эталонные копии до обновления лежат в: $BACKUP_DIR"
        warn "Stash сохранён: git stash list"
    fi
fi

# ───────── подстановка __AIOS_ROOT__ в settings*.json ─────────
# install.sh подставляет реальный путь вместо плейсхолдера; после обновления
# в новых файлах плейсхолдер может вернуться — подставляем заново.
b " Привожу настройки к этой установке"; hr
for sf in settings.json settings-sysadmin.json; do
    f="$BRIDGE/$sf"
    [[ -f "$f" ]] || continue
    if grep -q '__AIOS_ROOT__' "$f" 2>/dev/null; then
        sed "s|__AIOS_ROOT__|$AIOS_ROOT|g" "$f" > "$f.tmp" && mv "$f.tmp" "$f"
        ok "$sf: путь установки подставлен"
    fi
done

# ───────── тесты ─────────
rollback() {  # откат к коммиту до обновления + возврат stash
    err "Откатываюсь к предыдущей версии ($LOCAL_VER)…"
    git reset --hard "$LOCAL_REF" >/dev/null 2>&1 || true
    # восстановим конфиги из бэкапа (reset --hard их вернёт в git-состояние,
    # но локальные install-time значения берём из бэкапа для надёжности)
    for f in .env agents.toml settings.json settings-sysadmin.json; do
        [[ -f "$BACKUP_DIR/$f" ]] && cp -p "$BACKUP_DIR/$f" "$BRIDGE/$f"
    done
    restore_stash
    err "Откат выполнен. Система на версии $LOCAL_VER. Бэкап: $BACKUP_DIR"
}

if [[ -x "$PY" ]] && [[ -d "$AIOS_ROOT/tests" ]]; then
    b " Прогоняю тесты"; hr
    if "$PY" -m pytest "$AIOS_ROOT/tests/" -q; then
        ok "Тесты прошли"
    else
        err "Тесты упали на новой версии."
        rollback
        exit 1
    fi
else
    warn "venv или tests/ не найдены — пропускаю автотесты"
fi

# ───────── рестарт сервиса ─────────
b " Перезапускаю мост"; hr
restart_service() {
    case "$(uname -s)" in
        Linux)
            if command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files 2>/dev/null | grep -q '^aios-bridge.service'; then
                local sudo=""; [[ "$(id -u)" != "0" ]] && sudo="sudo"
                if $sudo -n true 2>/dev/null || [[ -z "$sudo" ]]; then
                    $sudo systemctl restart aios-bridge.service && return 0
                fi
                warn "Нет прав sudo для рестарта. Выполни: sudo systemctl restart aios-bridge"
                return 1
            fi
            warn "systemd-сервис aios-bridge не настроен — рестарт пропущен (запусти scripts/enable.sh)"
            return 1
            ;;
        Darwin)
            local label="com.aios-public.bridge"
            if launchctl list 2>/dev/null | grep -q "$label"; then
                launchctl kickstart -k "gui/$(id -u)/$label" && return 0
            fi
            warn "launchd-сервис $label не загружен — рестарт пропущен (запусти scripts/enable.sh)"
            return 1
            ;;
        *)
            warn "Неизвестная ОС — рестарт пропущен"
            return 1
            ;;
    esac
}
if restart_service; then
    ok "Мост перезапущен"
fi

# ───────── итог ─────────
hr
b " ✅ Обновление завершено"
ok "Версия:  $LOCAL_VER → $REMOTE_VER"
ok "Бэкап:   $BACKUP_DIR"
if [[ -n "$CONFLICTS" ]]; then
    warn "Внимание: были конфликты при возврате твоих правок — проверь файлы выше и бэкап."
fi
echo ""
echo " Если что-то пошло не так — откат: git reset --hard \$(cat $BACKUP_DIR/PREV_COMMIT) && restore конфигов из $BACKUP_DIR"
