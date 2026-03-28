# QUORBIT — Модели и сеть
## Текущее состояние и решения
## 2026-03-28 (обновлено)

---

## АГЕНТЫ PROMPTFORGE (10 штук)

| ID | Модель | Провайдер | Статус | Проблема |
|----|--------|-----------|--------|----------|
| local | Llama 3.2 1B | Ollama | ❌ | host.docker.internal DNS из Docker |
| cerebras | Llama 3.1 8B | Cerebras | ✅ | Бесплатно, 1M tok/day |
| sambanova | Llama 3.3 70B | SambaNova | ✅ | Бесплатно |
| deepseek | DeepSeek V3 | DeepSeek | ❌ | 402 Insufficient Balance |
| grok | Grok 2 | xAI | ❌ | 403 No credits |
| mistral | Mistral Small 24B | OpenRouter free | ⚠️ | Rate limit 429 |
| llama70b | Nemotron 120B | OpenRouter free | ✅ | Стабильно |
| qwen | Qwen3 4B | OpenRouter free | ⚠️ | Rate limit 429 |
| groq | Llama 3.3 70B | Groq | ✅ | Работает |
| gemini_direct | Gemini 2.0 Flash | Google | ⚠️ | 429 quota exceeded |

**Стабильные (4):** Cerebras, SambaNova, Nemotron/OpenRouter, Groq
**Иногда (3):** Mistral, Qwen, Gemini
**Не работают (3):** Local (DNS), DeepSeek (баланс), Grok (кредиты)

---

## API КЛЮЧИ

| Провайдер | Env Variable | Статус | Используется в |
|-----------|-------------|--------|----------------|
| OpenRouter | OPENROUTER_API_KEY | ✅ Есть | PF + Radar |
| Groq | GROQ_API_KEY | ✅ Есть | PF + Radar |
| Gemini | GEMINI_API_KEY | ✅ Есть | PF + Radar |
| DeepSeek | DEEPSEEK_API_KEY | ⚠️ Баланс=0 | PF + Radar |
| Grok/xAI | GROK_API_KEY | ⚠️ Нет кредитов | PF |
| Cerebras | CEREBRAS_API_KEY | ✅ Есть | PF + Radar |
| SambaNova | SAMBANOVA_API_KEY | ✅ Есть | PF + Radar |
| Mistral | MISTRAL_API_KEY | ✅ В GitHub Secrets | Radar |
| Together | TOGETHER_API_KEY | ⬜ Нет | Radar |
| Cohere | COHERE_API_KEY | ⬜ Нет | Radar |
| Fireworks | FIREWORKS_API_KEY | ⬜ Нет | Radar |
| Cloudflare | CLOUDFLARE_API_KEY | ⬜ Нет | Radar |
| Telegram | TELEGRAM_BOT_TOKEN | ✅ В GitHub Secrets | Radar бот |

---

## FREEAPIRADAR — ПОСЛЕДНИЙ /status

```
🟢 Groq          conf:72  ping:290ms  18 models
🟢 Cerebras      conf:72  ping:143ms  2 models
🟢 SambaNova     conf:72  ping:313ms  17 models
🟡 Google AI     conf:25  ping:255ms  33 models
🔴 OpenRouter    conf:0   ping:130ms  346 models
⚪ DeepSeek      conf:50  ping:—
⚪ xAI           conf:50  ping:—
⚪ Mistral       conf:50  ping:—
⚪ Together      conf:50  ping:—
⚪ Cohere        conf:50  ping:—
⚪ Fireworks     conf:50  ping:—
⚪ Cloudflare    conf:50  ping:—
```

3/12 стабильно working. 7 без ключей, 1 degraded, 1 down.

---

## VPN/Location — решённые проблемы

- Groq: работает (раньше блокировал датацентровые IP)
- Docker DNS: решено через dns: 8.8.8.8 в docker-compose
- Ollama из Docker: НЕ решено (host.docker.internal), приоритет НИЗКИЙ

---

## Локальные модели

- Ollama установлен, llama3.2:1b скачан
- НЕ используется (Docker DNS проблема)
- Приоритет: НИЗКИЙ (4 cloud-агента достаточно)

---

## DOCKER ПОРТЫ

| Сервис | PromptForge | QUORBIT | FreeAPIRadar |
|--------|-------------|---------|--------------|
| Frontend | 3000 | — | GitHub Pages |
| Backend API | 8000 | 8001 | GitHub Actions |
| Redis | 6379 | 6380 | — |
| PostgreSQL | — | 5432 | — |
| Ollama | 11434 (host) | — | — |

---

## ДЕЙСТВИЯ (по приоритету)

### Сделано ✅:
1. ✅ DeepSeek агент добавлен (баланс=0)
2. ✅ Grok/xAI агент добавлен (нет кредитов)
3. ✅ Cerebras + SambaNova — работают, бесплатно
4. ✅ FreeAPIRadar MVP на GitHub
5. ✅ Telegram-бот работает
6. ✅ GitHub Actions + Secrets настроены

### Осталось:
7. ⬜ Зарегистрироваться: Cohere, Fireworks → ключи в Secrets
8. ⬜ DeepSeek: положить $1-2 → починить агента
9. ⬜ Show HN + Reddit пост
10. ⬜ E2E demo: Radar → QUORBIT → PromptForge

---

*Документ обновлён: 2026-03-28*
