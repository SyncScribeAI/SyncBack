FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv/app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libpq-dev libmagic1 \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml poetry.lock* requirements.txt* ./
RUN pip install --upgrade pip && \
    if [ -f requirements.txt ]; then pip install -r requirements.txt; else pip install .; fi

COPY app ./app

RUN groupadd --system syncscribe && useradd --system --gid syncscribe --home /srv/app syncscribe \
    && chown -R syncscribe:syncscribe /srv/app
USER syncscribe

# Общая очередь. Анализ v2 слушает отдельный воркер (сервис worker-analysis в docker-compose.yml)
# с той же командой, но -Q ANALYSIS_V2_QUEUE и --concurrency по числу слотов ML-конвейера.
CMD ["celery", "-A", "app.workers.celery_app", "worker", \
     "--loglevel=INFO", "-Q", "celery", "--concurrency=4"]
