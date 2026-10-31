# syntax=docker/dockerfile:1
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    ALLOW_LIVE_TRADING=FALSE

WORKDIR /app

# System deps for LightGBM (libgomp1) and general runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src
COPY config ./config
COPY scripts ./scripts

RUN pip install --upgrade pip && pip install .

EXPOSE 8080

CMD ["python", "scripts/run_engine.py"]
