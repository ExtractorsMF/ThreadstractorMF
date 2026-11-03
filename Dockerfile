# syntax=docker/dockerfile:1
ARG PYTHON_VERSION=3.12

# ---- base ----
FROM python:${PYTHON_VERSION}-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
WORKDIR /app

# ---- slim (core only) ----
FROM base AS slim
COPY pyproject.toml uv.lock README.md ./
COPY threadstractormf ./threadstractormf
RUN uv sync --frozen --no-dev
ENV PATH="/app/.venv/bin:$PATH"
USER 1000
ENTRYPOINT ["threadstractormf"]

# ---- full (slim + antibot + browser) ----
FROM slim AS full
USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
        libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
        libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
        libgbm1 libasound2 libpango-1.0-0 libcairo2 \
    && rm -rf /var/lib/apt/lists/*
RUN uv sync --frozen --no-dev --extra browser --extra antibot
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN uv run playwright install chromium && chmod -R 755 /ms-playwright
ENV THREADS_USE_SANDBOX=1
ENV PATH="/app/.venv/bin:$PATH"
USER 1000
ENTRYPOINT ["threadstractormf"]
