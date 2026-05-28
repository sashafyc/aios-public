# Runner'ы — на чём работают агенты

Runner = бэкенд LLM. Указывается в agents.toml: `runner_type` + `model`.

## Доступные

| runner_type | Модели | Авторизация | Стоимость | stream |
|---|---|---|---|---|
| `claude` | claude-sonnet-4-6, claude-opus-4-6 | `claude auth` (подписка/API) | подписка или per-token | ✅ |
| `codex` | gpt-5.4-codex, o4-mini | `codex auth` (ChatGPT Plus) | ~$20/мес | ❌ |
| `deepseek` | deepseek-v4-pro, deepseek-v4-flash | `DEEPSEEK_API_KEY` в .env | ~$0.5/M токенов | ✅ |
| `gemini` | (авто Pro/Flash) | `gemini auth` (бесплатно) | бесплатно, лимит/день | ❌ |

## Что выбрать

- **Claude** — лучший tool use (файлы, bash, навыки). Для Ассистента/Сисадмина/Скрайбера.
- **DeepSeek** — дёшево, для массовых текстовых задач. Слабее в tool use.
- **Codex** — если уже есть ChatGPT Plus.
- **Gemini** — бесплатно, для экспериментов.

## Подключение нового runner'а

### Claude / Codex / Gemini (через CLI)
1. Установить CLI (`claude` / `codex` / `gemini`).
2. Авторизоваться: `claude auth` / `codex auth` / `gemini auth` (откроется браузер).
3. На VPS под пользователем `aios`: `su - aios -c "claude auth"`.
4. Проверить: запустить агента.

### DeepSeek / OpenAI (через API key)
1. Получить ключ на платформе провайдера.
2. Прописать в `bridge/.env`: `DEEPSEEK_API_KEY=...` (или `OPENAI_API_KEY=...`).
3. Рестарт моста (правка .env требует рестарта).

## Переключение агента на другой runner

В `agents.toml` у нужного агента поменяй:
```toml
runner_type = "deepseek"
model = "deepseek-v4-pro"
stream = false              # deepseek/claude → true; codex/gemini → false
```
Hot-reload подхватит за 60 сек. Рестарт не нужен.

## Fallback cascade

Если основной runner упал (rate limit, ошибка) — мост автоматически пробует резервный (Codex → o4-mini). Блокировка до ближайших :00 или :30. Пользователь видит: «⚠️ Claude недоступен, переключён на Codex до 15:30».
