# QUORBIT ECOSYSTEM — Синтез трёх проектов
## Информационный файл проекта
## 2026-03-28 (обновлено)

---

## Три проекта, один стек

```
┌─────────────────────────────────────────┐
│  Layer 3: PromptForge                   │
│  User-facing мульти-агентный чат        │
│  10 агентов, React + WebSocket          │
│  4 стабильных (Cerebras, SambaNova,     │
│  Nemotron/OpenRouter, Groq)             │
└──────────────────┬──────────────────────┘
                   │ просит: "какой агент для задачи?"
                   ▼
┌─────────────────────────────────────────┐
│  Layer 2: QUORBIT Protocol              │
│  Оркестрация, scoring, routing          │
│  Discovery → Scoring → Delegation       │
│  Ed25519 identity, reputation, BusAI    │
│  Sprint 9.5 complete, Sprint 10 NEXT    │
└──────────────────┬──────────────────────┘
                   │ спрашивает: "какие API живы?"
                   ▼
┌─────────────────────────────────────────┐
│  Layer 1: FreeAPIRadar                  │
│  Data collection, change intelligence   │
│  Health checks, rate limits, changelog  │
│  GitHub Actions + Telegram-бот          │
│  Публичный open-source + B2B data      │
└─────────────────────────────────────────┘
```

---

## Почему это один стек, а не три отдельных проекта

### Проблема PromptForge:
- OpenRouter free: 429 каждые 2-3 запроса
- Groq: работает стабильно (фикс с Session 5)
- Gemini: квота 15 req/min, часто 429
- DeepSeek: баланс = 0, Grok: кредиты = 0
- Итог: из 10 агентов стабильно работают 4

### Как FreeAPIRadar решает это:
- Автоматически проверяет 12 провайдеров каждые 6 часов
- Знает, кто жив прямо сейчас, с какой latency, из какого региона
- PromptForge/QUORBIT читает эти данные и автоматически переключается на рабочего агента

### Как QUORBIT усиливает FreeAPIRadar:
- QUORBIT discovery = уже готовый Smart Router (parallel discovery, scoring v2, fallback)
- QUORBIT reputation score = аналог FreeAPIRadar confidence score
- Не нужно строить отдельный router — он есть в QUORBIT

### Как FreeAPIRadar питает QUORBIT:
- CapabilityCard._dynamic.sla_estimates.latency_p50 ← FreeAPIRadar ping data
- CapabilityCard._dynamic.current_load ← FreeAPIRadar rate_limits remaining
- CapabilityCard._dynamic.provider_health ← FreeAPIRadar confidence score
- Discovery scoring: `(1 - current_load) × 0.05` берёт данные из внешнего источника, а не self-reported

---

## FreeAPIRadar — текущее состояние

### Что это
Сервис, который показывает разработчикам, какие бесплатные AI API реально работают прямо сейчас, что изменилось, и что использовать вместо упавшего.

### Категория продукта
**Change Intelligence для бесплатных AI API**

### One-liner
"Don't guess. Know which free AI APIs work — right now."

### Текущий статус MVP
- ✅ GitHub repo: quorbit-labs/freeapiradar (public)
- ✅ GitHub Actions: cron каждые 6 часов, работает
- ✅ 12 адаптеров, 3/12 responding стабильно (Groq, Cerebras, SambaNova)
- ✅ Telegram-бот: /status, /subscribe, /providers, push alerts
- ✅ Subscriber storage (JSON), notifier (GitHub Actions mode)
- ⬜ Show HN + Reddit (Неделя 2)
- ⬜ Cohere, Fireworks, Mistral ключи (регистрация)
- ⬜ Лендинг + waitlist (Неделя 3)

### Источник решений
Идея проработана через 12 ответов от 6 AI моделей (Claude Opus 4.6, GPT o3, Perplexity, Grok, Kimi, DeepSeek). Полный анализ: `FreeAPIRadar-Action-Document.md`.

---

## Принятые решения (консенсус 6 моделей)

### Архитектура данных
- Change intelligence > ping monitoring (не публиковать точные лимиты)
- Fuzzy signals: 🟢/🟡/🔴 + confidence score + timestamp, не "30 RPM"
- Graduated access: 4 уровня (public → registered → contributors → partners)
- Change detection (70%) + ping-тесты (30%) — пропорция сигнала
- Задержка публикации 6-24 часа (решение Goodhart's Law)

### Продукт
- MVP = GitHub repo + CI/CD + Telegram-бот (не SaaS)
- 12 провайдеров: Groq, Google, DeepSeek, Mistral, Together, Cerebras, SambaNova, OpenRouter, xAI, Cohere, Fireworks, Cloudflare
- Provider-specific адаптеры (50-200 строк каждый)
- Без авторегистрации, без quality benchmarks, без Smart Router в MVP

### Бизнес
- Consumers = distribution (бесплатно, для роста)
- B2B = revenue (gateway providers $500-2000/мес, dev tools $1000-5000/мес)
- Affiliate-first monetization (нулевой friction)
- Подписка $9-29/мес параллельно
- TAM: $10-200K MRR (нишевой indie бизнес, не unicorn)

### Kill criteria
- < 100 GitHub stars за 30 дней
- < 30 email signups
- 0 affiliate clicks
- 0 issues/PRs от community
- < 5 реальных изменений у провайдеров за 30 дней

---

## Техническая архитектура FreeAPIRadar

```
freeapiradar/
├── bot/                         ← НОВОЕ (Session 6)
│   ├── __init__.py
│   ├── __main__.py
│   ├── config.py               # 12 провайдеров, emoji, URLs
│   ├── handlers.py             # /status, /subscribe, etc.
│   ├── main.py                 # Entry point (polling mode)
│   ├── notifier.py             # Push alerts (GitHub Actions mode)
│   └── storage.py              # JSON subscriber persistence
├── providers/
│   ├── base.py                 # ProviderAdapter interface
│   ├── openai_compat.py        # Base class (7 of 12 inherit)
│   ├── groq.py, deepseek.py, cerebras.py, sambanova.py,
│   │   xai.py, mistral.py, together.py, openrouter.py, fireworks.py
│   ├── google_ai.py            # Custom API (Gemini)
│   ├── cohere.py               # Custom API
│   └── cloudflare.py           # Stub
├── core/
│   ├── monitor.py              # основной скрипт
│   ├── diff_engine.py          # сравнение с предыдущим состоянием
│   ├── confidence.py           # confidence decay/recovery
│   └── readme_gen.py           # генерация README
├── data/
│   ├── status.json             # текущий статус (dict format)
│   ├── history/                # snapshots
│   └── changes.json            # лог изменений
├── .github/workflows/
│   └── monitor.yml             # cron + Telegram alerts
├── README.md
└── BOT_README.md
```

---

## Интеграция с QUORBIT

### Точки связи:
1. `freeapiradar/data/status.json` → QUORBIT discovery scoring (provider health data)
2. FreeAPIRadar confidence → CapabilityCard._dynamic.provider_health
3. FreeAPIRadar latency → CapabilityCard._dynamic.sla_estimates
4. FreeAPIRadar rate_limits → CapabilityCard._dynamic.current_load
5. FreeAPIRadar changes_log → QUORBIT alerting (переключение агентов)

### Интеграция с PromptForge:
1. PromptForge читает status.json при выборе агента
2. Если provider.status == "down" → агент исключается из UI
3. Если provider.confidence < 50 → агент помечается ⚠️
4. Auto-fallback: если текущий агент упал → следующий по confidence

---

## Связанные репозитории

| Проект | Repo | Статус |
|--------|------|--------|
| QUORBIT Protocol | github.com/quorbit-labs/core | Public, AGPL-3.0, Sprint 9.5 done |
| PromptForge | Локальный (C:\Projects\promptforge-v1) | Будет quorbit-labs/promptforge |
| FreeAPIRadar | github.com/quorbit-labs/freeapiradar | Public, AGPL-3.0, MVP working |

---

*Документ обновлён: 2026-03-28*
*Источник: deep research session в Claude Opus 4.6 + 12 ответов от 6 AI моделей*
