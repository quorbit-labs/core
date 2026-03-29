# QUORBIT — Модели и сеть
## Quick reference: агенты, ключи, порты, статусы
## 2026-03-29 (обновлено)

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

## BUSAI — ПАРАМЕТРЫ

| Параметр | Диапазон | Default | Что регулирует |
|----------|---------|---------|----------------|
| collusion_graph_weight | [10, 70] | 30 | Вес коллюзионного графа |
| graph_symmetry_weight | [10, 70] | 30 | Вес симметрии графа |
| sla_cliff_weight | [10, 70] | 30 | Вес SLA cliff |
| humangate_rate_limit | [5, 50] | 20 | Rate limit HumanGate |
| discovery_relaxation_ttl | [1.0, 3.0] | 1.0 | TTL relaxation discovery |
| reputation_ema_window | [50, 200] | 100 | Окно EMA репутации |

**Immutable (NEVER adjusted):** quorum_formula, quarantine_threshold, identity, genesis_config
**Poll interval:** 60 секунд
**Cooldown:** 15 min между adjustments одного параметра
**Freeze:** 1h если >3 adjustments за 30 min

---

## DOCKER ПОРТЫ

| Сервис | PromptForge | QUORBIT | FreeAPIRadar |
|--------|-------------|---------|--------------|
| Frontend | 3000 | — | GitHub Pages |
| Backend API | 8000 | 8001 (→8000 internal) | GitHub Actions |
| Redis | 6379 | 6380 (→6379 internal) | — |
| PostgreSQL | — | 5432 | — |
| Ollama | 11434 (host) | — | — |

**Примечание:** QUORBIT docker-compose маппит 8001→8000 (host→container). Dockerfile EXPOSE 8000. API доступен на localhost:8001.

---

## РЕШЁННЫЕ ПРОБЛЕМЫ

- Groq: работает (раньше блокировал датацентровые IP)
- Docker DNS: решено через `dns: 8.8.8.8` в docker-compose
- Ollama из Docker: НЕ решено (host.docker.internal), приоритет НИЗКИЙ
- Криптография: `cryptography` (hazmat Ed25519) — canonical, не PyNaCl

---

## ДЕЙСТВИЯ (по приоритету)

### Сделано ✅:
1. ✅ Все 10 агентов добавлены в PromptForge
2. ✅ Cerebras + SambaNova — работают, бесплатно
3. ✅ FreeAPIRadar MVP на GitHub
4. ✅ Telegram-бот работает
5. ✅ GitHub Actions + Secrets настроены
6. ✅ TELEGRAM_BOT_TOKEN в GitHub Secrets
7. ✅ Show HN + Reddit посты подготовлены
8. ✅ BusAI Sprint 8 complete (engine + cooldown)

### Осталось:
9. ⬜ Зарегистрироваться: Cohere, Fireworks → ключи в Secrets
10. ⬜ DeepSeek: положить $1-2 → починить агента
11. ⬜ Опубликовать Show HN + Reddit посты
12. ⬜ Sprint 10 QUORBIT: запуск e2e_demo.py с Docker
13. ⬜ E2E demo: Radar → QUORBIT → PromptForge
14. ⬜ PromptForge: дебаг новых агентов (не отображают ответы)

---

*Документ обновлён: 2026-03-29*
*Copyright (c) 2026 Quorbit Labs*
