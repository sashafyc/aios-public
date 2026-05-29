#!/usr/bin/env bash
# cron-sync.sh — применяет единый манифест scripts/crontab в реальный crontab.
#
# Манифест scripts/crontab — единственный источник правды по расписанию.
# Этот скрипт подставляет __AIOS_ROOT__ и вставляет содержимое в crontab внутри
# управляемого блока с маркерами, НЕ трогая остальные (чужие) строки crontab.
#
#   bash scripts/cron-sync.sh            применить манифест
#   bash scripts/cron-sync.sh --list     показать что запланировано (и установлено ли)
#   bash scripts/cron-sync.sh --dry-run  показать отрендеренный блок, не записывая
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AIOS_ROOT="${AIOS_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
MANIFEST="$AIOS_ROOT/scripts/crontab"

START_MARK="# >>> aios-public (managed by cron-sync.sh) — НЕ редактировать вручную, правь scripts/crontab >>>"
END_MARK="# <<< aios-public <<<"

b()  { printf '\033[1m%s\033[0m\n' "$*"; }
ok() { printf '   \033[32m✅ %s\033[0m\n' "$*"; }
err(){ printf '   \033[31m❌ %s\033[0m\n' "$*" >&2; }

[[ -f "$MANIFEST" ]] || { err "манифест не найден: $MANIFEST"; exit 1; }

# Отрендерить манифест: подставить реальный путь установки.
render() {
    local content
    content="$(cat "$MANIFEST")"
    printf '%s\n' "${content//__AIOS_ROOT__/$AIOS_ROOT}"
}

# Текущий crontab без нашего управляемого блока (чужие строки сохраняем).
crontab_without_block() {
    crontab -l 2>/dev/null | awk -v s="$START_MARK" -v e="$END_MARK" '
        $0==s {inblk=1; next}
        $0==e {inblk=0; next}
        !inblk {print}
    '
}

mode="${1:-apply}"

case "$mode" in
    --list)
        b "Единое расписание aios-public (источник: scripts/crontab)"
        render
        echo ""
        if crontab -l 2>/dev/null | grep -qF "$START_MARK"; then
            ok "блок установлен в crontab (crontab -l)"
        else
            err "блок ещё НЕ установлен — примени: bash scripts/cron-sync.sh"
        fi
        ;;
    --dry-run)
        b "Отрендеренный блок (запись НЕ выполняется):"
        printf '%s\n%s\n%s\n' "$START_MARK" "$(render)" "$END_MARK"
        ;;
    apply)
        if ! command -v crontab >/dev/null 2>&1; then
            err "crontab недоступен в этой системе — расписание не применено"
            exit 1
        fi
        b "Применяю единое расписание в crontab"
        new_crontab="$(crontab_without_block)
$START_MARK
$(render)
$END_MARK"
        printf '%s\n' "$new_crontab" | crontab -
        ok "crontab обновлён из scripts/crontab"
        echo "   Посмотреть: crontab -l   |   проверить: bash scripts/cron-sync.sh --list"
        ;;
    *)
        err "неизвестный аргумент: $mode (ожидается --list | --dry-run | без аргумента)"
        exit 1
        ;;
esac
