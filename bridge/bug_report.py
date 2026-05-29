"""
bug_report.py — сбор баг-репортов. Local-first, отправка по умолчанию ВКЛ (анонимно).

Приватность (важно):
  • Локальная запись — всегда (logs/bugs/bugs.jsonl), у пользователя своя история.
  • Отправка на сервер включена по умолчанию (AIOS_BUGREPORT=1) при заданном
    AIOS_BUGREPORT_URL. Выключается AIOS_BUGREPORT=0. Спрашивается при установке.
  • Редакция: пути (/home/<user>, /Users/<user>, $HOME) → ~, секреты маскируются
    (redact.redact_secrets), длина ограничена. НЕ собираем: содержимое файлов,
    переписку, имя пользователя, ключи.
  • install_id — случайный UUID (анонимный, для дедупа), не привязан к личности.

Использование:
  • Из моста (Python): bug_report.capture(source="error", description=..., error=...)
  • Из CLI (Сисадмин при «нашёл баг»):
        python bridge/bug_report.py --source user --desc "..." [--agent X] [--error "..."]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import platform
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from redact import redact_secrets

log = logging.getLogger("bridge.bug_report")

_BRIDGE_DIR = Path(__file__).resolve().parent
BUGS_DIR = _BRIDGE_DIR.parent / "logs" / "bugs"
BUGS_LOG = BUGS_DIR / "bugs.jsonl"
SENT_FILE = BUGS_DIR / ".sent"
INSTALL_ID_FILE = _BRIDGE_DIR.parent / "logs" / ".install_id"

MAX_FIELD = 4000  # лимит длины текстовых полей в отчёте


def _version() -> str:
    """Версия моста — парсим из tg_bridge.py без импорта (чтобы без сайд-эффектов)."""
    try:
        t = (_BRIDGE_DIR / "tg_bridge.py").read_text(encoding="utf-8")
        m = re.search(r'BRIDGE_VERSION\s*=\s*"([^"]+)"', t)
        return m.group(1) if m else "?"
    except Exception:
        return "?"


def install_id() -> str:
    """Анонимный стабильный id установки (для дедупа на сервере). Не про личность."""
    try:
        if INSTALL_ID_FILE.exists():
            return INSTALL_ID_FILE.read_text(encoding="utf-8").strip()
        INSTALL_ID_FILE.parent.mkdir(parents=True, exist_ok=True)
        new_id = uuid.uuid4().hex
        INSTALL_ID_FILE.write_text(new_id, encoding="utf-8")
        return new_id
    except Exception:
        return "anon"


def scrub(text: str | None) -> str:
    """Убрать секреты и пути, ведущие к личности; ограничить длину."""
    if not text:
        return ""
    t = redact_secrets(str(text))
    home = str(Path.home())
    if home and home != "/":
        t = t.replace(home, "~")
    # любые /home/<user>/ и /Users/<user>/ → ~/
    t = re.sub(r"/home/[^/\s]+", "~", t)
    t = re.sub(r"/Users/[^/\s]+", "~", t)
    if len(t) > MAX_FIELD:
        t = t[:MAX_FIELD] + "…[truncated]"
    return t


def build_report(*, source: str, description: str, error: str | None = None,
                 agent: str | None = None, runner: str | None = None,
                 error_kind: str | None = None, version: str | None = None) -> dict:
    return {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "install_id": install_id(),
        "version": version or _version(),
        "os": f"{platform.system()} {platform.release()}",
        "python": platform.python_version(),
        "source": source,                 # "user" (репорт пользователя) | "error" (авто)
        "agent": agent or "",
        "runner": runner or "",
        "error_kind": error_kind or "",
        "description": scrub(description),
        "error": scrub(error),
    }


def _signature(report: dict) -> str:
    base = f"{report.get('source')}|{report.get('error_kind')}|{report.get('error','')[:200]}|{report.get('description','')[:120]}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:16]


def record(report: dict) -> Path:
    """Записать отчёт локально (всегда). Возвращает путь к логу."""
    try:
        BUGS_DIR.mkdir(parents=True, exist_ok=True)
        with open(BUGS_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(report, ensure_ascii=False) + "\n")
    except Exception:
        log.exception("bug_report: local write failed")
    return BUGS_LOG


def _already_sent(sig: str) -> bool:
    try:
        return SENT_FILE.exists() and sig in SENT_FILE.read_text(encoding="utf-8").split()
    except Exception:
        return False


def _mark_sent(sig: str) -> None:
    try:
        BUGS_DIR.mkdir(parents=True, exist_ok=True)
        with open(SENT_FILE, "a", encoding="utf-8") as f:
            f.write(sig + "\n")
    except Exception:
        pass


def send(report: dict) -> bool:
    """Отправить отчёт на сервер, ЕСЛИ включено в .env. Иначе — no-op (False).
    Авто-ошибки (source=error) дедупим по сигнатуре; репорты пользователя шлём всегда."""
    if os.getenv("AIOS_BUGREPORT", "1") != "1":
        return False
    url = os.getenv("AIOS_BUGREPORT_URL", "").strip()
    if not url:
        return False
    sig = _signature(report)
    if report.get("source") == "error" and _already_sent(sig):
        return False
    try:
        data = json.dumps(report, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 (url из .env владельца)
            ok = 200 <= resp.status < 300
        if ok:
            _mark_sent(sig)
        return ok
    except Exception as exc:
        log.info("bug_report: send failed (%s) — отчёт сохранён локально", exc)
        return False


def capture(*, source: str, description: str, error: str | None = None,
            agent: str | None = None, runner: str | None = None,
            error_kind: str | None = None, version: str | None = None) -> dict:
    """Собрать + записать локально + отправить (если включено). Возвращает отчёт.
    Безопасно вызывать откуда угодно — исключения не пробрасываются."""
    try:
        report = build_report(source=source, description=description, error=error,
                              agent=agent, runner=runner, error_kind=error_kind, version=version)
        record(report)
        send(report)
        return report
    except Exception:
        log.exception("bug_report: capture failed")
        return {}


def main() -> int:
    ap = argparse.ArgumentParser(description="Записать баг-репорт (local-first, отправка по opt-in)")
    ap.add_argument("--source", default="user", choices=["user", "error"])
    ap.add_argument("--desc", required=True, help="описание проблемы (тех. языком)")
    ap.add_argument("--agent", default="")
    ap.add_argument("--runner", default="")
    ap.add_argument("--error", default="")
    ap.add_argument("--kind", default="")
    args = ap.parse_args()
    rep = capture(source=args.source, description=args.desc, error=args.error or None,
                  agent=args.agent or None, runner=args.runner or None, error_kind=args.kind or None)
    sent = os.getenv("AIOS_BUGREPORT", "1") == "1" and bool(os.getenv("AIOS_BUGREPORT_URL", "").strip())
    print(f"Баг записан локально ({BUGS_LOG}).", "Отправлен на сервер." if sent else "Отправка выключена (local-only).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
