# QUORBIT ECOSYSTEM — Синтез четырёх проектов
## Информационный файл проекта
## 2026-03-29 (обновлено)

---

## Четыре проекта, один стек

```
┌─────────────────────────────────────────┐
│  Layer 4: PromptForge                   │
│  User-facing мульти-агентный чат        │
│  10 агентов, React + WebSocket          │
│  4 стабильных (Cerebras, SambaNova,     │
│  Nemotron/OpenRouter, Groq)             │
└──────────────────┬──────────────────────┘
                   │ просит: "какой агент для задачи?"
                   ▼
┌─────────────────────────────────────────┐
│  Layer 3: QUORBIT Protocol              │
│  Trust layer: identity, reputation,     │
│  consensus, discovery, anti-gaming      │
│  Ed25519 (cryptography hazmat)          │
│  Sprint 9.5 complete, Sprint 10 NEXT    │
└────────┬─────────────────┬──────────────┘
         │                 │
         ▼                 ▼
┌────────────────┐ ┌──────────────────────┐
│  Layer 2:      │ │  Layer 2:            │
│  BusAI         │ │  FreeAPIRadar        │
│  Adaptive      │ │  Change intelligence │
│  parameter     │ │  12 провайдеров      │
│  engine        │ │  GitHub Actions +    │
│  Proprietary   │ │  Telegram-бот        │
│  Sprint 8 done │ │  AGPL-3.0            │
└────────────────┘ └──────────────────────┘
```

**BusAI** наблюдает за метриками QUORBIT (collusion, queue depth, false quarantine rate) и подстраивает bounded параметры (anti-gaming weights, rate limits, discovery TTL). Без ML, без изменения топологии — только числа.

**FreeAPIRadar** поставляет внешние данные о провайдерах: latency, confidence, model count. QUORBIT использует эти данные для discovery scoring.

---

## Почему это один стек, а не четыре отдельных проекта

### Проблема PromptForge:
- OpenRouter free: 429 каждые 2-3 запроса
- Groq: работает стабильно
- Gemini: квота 15 req/min, часто 429
- DeepSeek: баланс = 0, Grok: кредиты = 0
- Итог: из 10 агентов стабильно работают 4

### Как FreeAPIRadar решает это:
- Автоматически проверяет 12 провайдеров каждые 6 часов
- Знает, кто жив прямо сейчас, с какой latency
- PromptForge/QUORBIT читает эти данные и переключается на рабочего агента

### Как QUORBIT усиливает FreeAPIRadar:
- QUORBIT discovery = готовый Smart Router (parallel discovery, scoring v2, fallback)
- QUORBIT reputation score = аналог FreeAPIRadar confidence score

### Как BusAI стабилизирует QUORBIT:
- Мониторит метрики каждые 60 секунд
- Если collusion_detections растут → увеличивает graph_weight
- Если false_quarantine_rate высокий → снижает threshold
- Всё в bounded ranges, всё в Merkle audit log
- Cooldown 15 min + freeze 1h для антиосцилляции

### Как FreeAPIRadar питает QUORBIT:
- CapabilityCard._dynamic.sla_estimates.latency_p50 ← FreeAPIRadar ping data
- CapabilityCard._dynamic.current_load ← FreeAPIRadar rate_limits remaining
- CapabilityCard._dynamic.provider_health ← FreeAPIRadar confidence score
- Discovery scoring: `(1 - current_load) × 0.05` берёт данные из внешнего источника

---

## FreeAPIRadar — текущее состояние

### Категория продукта
**Change Intelligence для бесплатных AI API**

### One-liner
"Don't guess. Know which free AI APIs work — right now."

### Текущий статус MVP
- ✅ GitHub repo: quorbit-labs/freeapiradar (public, AGPL-3.0)
- ✅ GitHub Actions: cron каждые 6 часов, работает
- ✅ 12 адаптеров, 3/12 responding стабильно (Groq, Cerebras, SambaNova)
- ✅ Telegram-бот: /status, /subscribe, /providers, push alerts
- ✅ TELEGRAM_BOT_TOKEN в GitHub Secrets
- ✅ Show HN + Reddit посты подготовлены
- ⬜ Cohere, Fireworks ключи (регистрация)
- ⬜ Лендинг + waitlist

### Принятые решения (из Action Document)
- Change intelligence > ping monitoring (не публиковать точные лимиты)
- Fuzzy signals: 🟢/🟡/🔴 + confidence score, не "30 RPM"
- Задержка публикации 6-24 часа (Goodhart's Law)
- MVP = GitHub repo + CI/CD + Telegram-бот (не SaaS)
- B2B = revenue (gateway providers $500-2000/мес)
- TAM: $10-200K MRR (нишевой indie бизнес)

---

## Интеграция между проектами

### FreeAPIRadar → QUORBIT:
1. `data/status.json` → QUORBIT discovery scoring (provider health)
2. confidence → CapabilityCard._dynamic.provider_health
3. latency → CapabilityCard._dynamic.sla_estimates
4. rate_limits → CapabilityCard._dynamic.current_load
5. changes_log → QUORBIT alerting (переключение агентов)

### QUORBIT → PromptForge:
1. Discovery scoring выбирает лучшего агента для задачи
2. Reputation отсекает ненадёжных агентов
3. BFT consensus валидирует результаты

### BusAI → QUORBIT:
1. Наблюдает метрики: collusion, queue depth, false quarantine rate
2. Подстраивает: anti-gaming weights, rate limits, discovery TTL
3. Bounded ranges, Merkle audit, cooldown/freeze

### PromptForge → FreeAPIRadar:
1. Реальные ошибки агентов (429, 402, timeout) = ground truth
2. Если provider.status == "down" → агент исключается из UI
3. Если provider.confidence < 50 → агент помечается ⚠️

---

## Связанные репозитории

| Проект | Repo | Лицензия | Статус |
|--------|------|----------|--------|
| QUORBIT Protocol | quorbit-labs/core | AGPL-3.0 | Sprint 9.5 done, Sprint 10 NEXT |
| FreeAPIRadar | quorbit-labs/freeapiradar | AGPL-3.0 | MVP working, 3/12 providers |
| PromptForge | quorbit-labs/promptforge | Private | 10 агентов, 4 стабильных |
| BusAI | quorbit-labs/busai | Proprietary | Sprint 8 complete |

## Docker порты

| Сервис | PromptForge | QUORBIT | FreeAPIRadar |
|--------|-------------|---------|--------------|
| Frontend | 3000 | — | GitHub Pages |
| Backend API | 8000 | 8001 (→8000) | GitHub Actions |
| Redis | 6379 | 6380 (→6379) | — |
| PostgreSQL | — | 5432 | — |

---

## Криптография

Canonical: `cryptography` (hazmat Ed25519). Без PyNaCl, без сторонних crypto-библиотек.
- Identity: `backend/app/bus/identity.py` — AgentIdentity, sign, verify
- SDK: `sdk/python/quorbit/client.py` — _LightIdentity fallback
- E2E demo: `e2e_demo.py`
- Зависимость: `cryptography>=42.0`

---

*Документ обновлён: 2026-03-29*
*Copyright (c) 2026 Quorbit Labs*
