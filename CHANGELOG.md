# Changelog

Все заметные изменения проекта. Формат — [Keep a Changelog](https://keepachangelog.com/ru/),
версии по [SemVer](https://semver.org/lang/ru/): `MAJOR.MINOR.PATCH`.

- **PATCH** (1.0.x) — фиксы, мелкие улучшения, без ломающих изменений.
- **MINOR** (1.x.0) — новые возможности, обратно совместимо.
- **MAJOR** (x.0.0) — ломающие изменения (миграция обязательна).

Релиз публикуется git-тегом `vX.Y.Z`. Как выкатывать — см. [docs/RELEASING.md](docs/RELEASING.md).

## [Unreleased]

_Сюда попадают изменения, которые уедут в следующий публичный релиз._

## [1.0.0] — 2026-05-29

Первый публичный релиз.

### Возможности
- Один Telegram-бот, много топиков — каждый топик это отдельный AI-агент.
- Три агента из коробки: Ассистент (универсал-хаб), Сисадмин (управление системой через диалог), Скрайбер (транскрибация).
- Любой LLM-бэкенд: Claude, Codex/GPT, DeepSeek, Gemini — выбор при установке.
- Streaming ответов, делегация между агентами (с лимитом глубины), auto-fallback при сбое раннера, hot-reload конфига, daily reset, keep-alive кэша, изолированные cron-сессии.
- Голосовые/аудио/видео → текст (AssemblyAI).
- Установка одной командой (`install.sh`), автозапуск (systemd/launchd), watchdog, doctor.
- Единое расписание (`scripts/crontab` + `cron-sync.sh`): мониторинг, обслуживание, проверка обновлений, задачи агентов.
- Безопасное обновление (`scripts/update.sh`): бэкап, тесты, авто-откат; пользовательские настройки и агенты не сносятся.

[Unreleased]: https://github.com/sashafyc/aios-public/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/sashafyc/aios-public/releases/tag/v1.0.0
