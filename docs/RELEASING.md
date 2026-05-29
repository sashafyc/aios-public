# Releasing — как выкатывать новую версию

Для мейнтейнера. Модель: **один публичный репозиторий**, разработка в нём (или
cherry-pick из приватного оригинала), релиз публикуется git-тегом `vX.Y.Z`.
У пользователей `scripts/update.sh` сравнивает их версию с `origin/main` по тегам.

## Версионирование (SemVer)

- **PATCH** `1.0.x` — фиксы, мелочи, без ломающих изменений.
- **MINOR** `1.x.0` — новые возможности, обратная совместимость.
- **MAJOR** `x.0.0` — ломающие изменения (нужна миграция).

Текущая версия — в `bridge/tg_bridge.py` (`BRIDGE_VERSION`) и в git-тегах.

## Чеклист выкатки

1. **Что было в последней публичной версии:** `git tag --list` → последний `vX.Y.Z`.
2. **Собрать все изменения после неё:** `git log vX.Y.Z..HEAD --no-merges`.
   Решить тип релиза (patch/minor/major).
3. **Обновить `bridge/tg_bridge.py`:** `BRIDGE_VERSION = "X.Y.Z"`.
4. **`CHANGELOG.md`:** перенести накопленное из `## [Unreleased]` в `## [X.Y.Z] — ДАТА`,
   сгруппировать (Возможности / Исправления / Изменения). Оставить пустой `[Unreleased]`.
5. **Рекомендации агентам (если есть):** если релиз улучшает поведение «мягких»
   агентов — добавить секцию `## vX.Y.Z` в `knowledge/system/upgrade-notes.md`
   (что и каким агентам дописать). Если нет — пропустить.
6. **Прогнать тесты:** `.venv/bin/python -m pytest tests/ -q` (должно быть зелёным).
7. **Коммит:** `git commit -am "release: vX.Y.Z"`.
8. **Тег + пуш:** `git tag vX.Y.Z && git push origin main --tags`.
9. **Готово.** У пользователей недельный `update.sh --check` (cron) увидит новую версию
   и пришлёт уведомление в Telegram; при согласии Сисадмин обновит безопасно
   (бэкап + тесты + откат), changelog придёт в топик Сисадмина.

## Что переживает обновление у пользователя (НЕ трогать в релизе с расчётом на снос)

- `bridge/.env`, `bridge/agents.toml` — gitignored, user-owned.
- Инструкции «мягких» агентов (`agents/assistant/CLAUDE.md`, `agents/scriber/CLAUDE.md`,
  кастомные) — gitignored; улучшения доставляются через `upgrade-notes.md`, не перезаписью.
- Память агентов (`context.md`/`journal.md`) — gitignored.

«Твёрдое» (едет с релизом и обновляется у всех): `bridge/`, `knowledge/`, `skills/`,
`scripts/`, `agents/_shared/`, `agents/_template/`, `agents/sysadmin/CLAUDE.md` + `router.md`.

## Откат релиза

Плохой тег: `git tag -d vX.Y.Z && git push origin :refs/tags/vX.Y.Z`, поправить, перевыпустить.
У пользователя откат делает сам `update.sh` (тесты упали → автоматический reset к прошлой версии).
