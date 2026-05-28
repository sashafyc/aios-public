#!/usr/bin/env bash
# doctor-short.sh — короткий health-check для cron.
# Запускает doctor.py и пишет одну строку-итог в лог. При errors — exit 1.
#   Cron (опц.):  0 */2 * * *  bash /path/aios-public/scripts/monitoring/doctor-short.sh
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AIOS_ROOT="${AIOS_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
PY="$AIOS_ROOT/.venv/bin/python"
LOG="$AIOS_ROOT/logs/bridge/doctor.log"
mkdir -p "$(dirname "$LOG")"

[[ -x "$PY" ]] || PY="python3"

out="$("$PY" "$AIOS_ROOT/bridge/doctor.py" 2>&1)"
rc=$?
summary="$(printf '%s\n' "$out" | grep -E '✅ [0-9]+ ok' | tail -1)"
printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "${summary:-doctor failed}" >> "$LOG"
exit $rc
