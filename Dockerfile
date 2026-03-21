FROM python:3.11-slim

LABEL org.opencontainers.image.title="QUORBIT Protocol API"
LABEL org.opencontainers.image.description="Trust layer for AI agents"
LABEL org.opencontainers.image.source="https://github.com/quorbit-labs/core"
LABEL org.opencontainers.image.licenses="AGPL-3.0"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install system dependencies for psycopg2 and cryptography
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq-dev gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ backend/

# Non-root user
RUN adduser --disabled-password --gecos "" quorbit && chown -R quorbit /app
USER quorbit

EXPOSE 8000

CMD ["uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
