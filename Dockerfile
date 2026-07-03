FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_NO_CACHE=1 \
    PATH="/app/.venv/bin:${PATH}" \
    PORT=8080

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY . .
RUN python -m pip install --upgrade pip uv \
    && uv sync --frozen --no-dev \
    && apt-get purge -y git \
    && apt-get autoremove -y --purge \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 appuser \
    && chown -R appuser:appuser /app

USER appuser

CMD ["gunicorn", "--bind", ":8080", "--workers", "1", "--threads", "1", "--timeout", "300", "main:app"]
