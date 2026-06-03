# Runner'ы — на чём работают агенты

Runner = бэкенд LLM. Указывается в agents.toml: `runner_type` + `model`.

> Какой агент на каком раннере и модели СЕЙЧАС, с ценами — **[[knowledge/system/agents-runners]]**
> (автоген из `agents.toml` + `bridge/pricing.py`, руками не правят).

## Доступные

| runner_type | Модели | CLI / авторизация | Стоимость | сессии | tools | stream |
|---|---|---|---|---|---|:---:|
| `claude` | claude-sonnet-4-6, claude-opus-4-6 | `claude` CLI (`claude auth`) | подписка / per-token | ✅ `--resume` | ✅ | ✅ |
| `codex` | gpt-5.4, gpt-5.5-codex | `codex` CLI (ChatGPT Plus) | ~$20/мес | ✅ `exec resume` | ✅ | ❌ |
| `deepseek` | deepseek-v4-pro | `codewhale` CLI + `DEEPSEEK_API_KEY` | ~$0.44/$0.87 за 1M (payg) | ✅ `--resume` | ✅ | ✅ |
| `gemini` | (авто Pro/Flash) | `gemini` CLI (`gemini auth`) | бесплатно, лимит/день | ✅ per-workdir | ⚠️ | ❌ |

Цены — единым прайс-листом в `bridge/pricing.py`. Раннеры считают `cost_usd` из него (claude берёт точный cost из CLI).

## Что выбрать

- **Claude** — лучший tool use (файлы, bash, навыки). Для Ассистента/Сисадмина/Скрайбера.
- **DeepSeek** — полноценный агент (руки + память диалога), но в ~15–20× дешевле Claude. Для массовых задач, где нужны инструменты, но Claude избыточен.
- **Codex** — если уже есть ChatGPT Plus.
- **Gemini** — бесплатно, самый большой контекст; слабее в tool use.

## Подключение нового runner'а

### Claude / Codex / Gemini (через CLI)
1. Установить CLI (`claude` / `codex` / `gemini`).
2. Авторизоваться: `claude auth` / `codex auth` / `gemini auth` (откроется браузер).
3. На VPS под пользователем `aios`: `su - aios -c "claude auth"`.
4. Проверить: запустить агента.

### DeepSeek (через CodeWhale CLI)
1. Установить CLI: `npm i -g codewhale` (преемник deprecated `deepseek-tui`; бинарь `/usr/bin/codewhale`).
2. Прописать ключ в `bridge/.env`: `DEEPSEEK_API_KEY=...`.
3. Рестарт моста (правка .env требует рестарта).

> DeepSeek-раннер работает через `codewhale exec --auto --output-format stream-json --model deepseek-v4-pro [--resume <sid>]` — то есть это настоящий агент с инструментами (`--auto`: чтение/запись файлов, shell), а не chat-API.

## Переключение агента на другой runner

В `agents.toml` у нужного агента поменяй:
```toml
runner_type = "deepseek"
model = "deepseek-v4-pro"
stream = true               # claude/deepseek стримят; codex/gemini → false
```
Hot-reload подхватит за ≤60 сек. Рестарт не нужен. При reload карта раннеров
(`knowledge/system/agents-runners.md`) **перегенерится автоматически** (hook → `scripts/gen-runners-doc.py`) — доки не рассинхронятся с конфигом.

## Сессии (память диалога)

Все четыре раннера держат **persistent-сессии** — контекст переживает рестарт моста:
- **claude / codex / deepseek** — CLI хранит сессию, мост возобновляет по `--resume <session_id>` (id сохраняется per-agent).
- **gemini** — сессия per-workdir (`--resume latest`).

## Fallback cascade

Если основной runner упал (rate limit, ошибка) — мост автоматически пробует резервный. Блокировка до ближайших :00 или :30. Пользователь видит: «⚠️ Claude недоступен, переключён на Codex до 15:30».
